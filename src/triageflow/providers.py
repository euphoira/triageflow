"""Bounded, asynchronous adapter for Chat Completions function-calling models.

The model and endpoint are explicitly configured by the user. Compatibility with
Chat Completions and function calling is required; this is not a Responses adapter.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

MAX_REQUEST_BYTES = 262_144
MAX_RESPONSE_BYTES = 1_048_576
MAX_ARGUMENT_BYTES = 16_384
MAX_TOOL_CALLS = 32
MAX_MESSAGES = 256
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 2.0


class ProviderError(RuntimeError):
    """A safe-to-display provider failure without credentials or response bodies."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Completion:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


def _decode_json(value: bytes | str) -> object:
    try:
        return json.loads(value, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProviderError("Provider returned malformed JSON.") from None


def _token_count(usage: dict, field_name: str) -> int:
    value = usage.get(field_name, 0)
    if type(value) is not int or value < 0:
        raise ProviderError("Provider returned invalid token usage.")
    return value


def _parse_completion(raw: bytes) -> Completion:
    data = _decode_json(raw)
    if not isinstance(data, dict):
        raise ProviderError("Provider returned an invalid completion object.")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError("Provider returned no valid completion choice.")
    choice = choices[0]
    if choice.get("finish_reason") == "length":
        raise ProviderError("Provider output exceeded its generation limit.")
    if choice.get("finish_reason") == "content_filter":
        raise ProviderError("Provider filtered this completion.")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        raise ProviderError("Provider returned an invalid assistant message.")
    if message.get("refusal"):
        raise ProviderError("Provider declined this completion.")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ProviderError("Provider returned invalid message content.")
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list) or len(raw_calls) > MAX_TOOL_CALLS:
        raise ProviderError("Provider returned an invalid number of tool calls.")
    calls: list[ToolCall] = []
    seen_ids: set[str] = set()
    for call in raw_calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ProviderError("Provider returned an invalid tool call.")
        call_id = call.get("id")
        function = call.get("function")
        if (
            not isinstance(call_id, str)
            or not call_id
            or len(call_id) > 200
            or call_id in seen_ids
            or not isinstance(function, dict)
        ):
            raise ProviderError("Provider returned an invalid tool call identifier.")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
            raise ProviderError("Provider returned an invalid tool name.")
        if not isinstance(arguments, str):
            raise ProviderError("Provider returned invalid tool arguments.")
        try:
            arguments_size = len(arguments.encode("utf-8"))
        except UnicodeError:
            raise ProviderError("Provider returned invalid tool arguments.") from None
        if arguments_size > MAX_ARGUMENT_BYTES:
            raise ProviderError("Provider tool arguments exceeded the size limit.")
        parsed_arguments = _decode_json(arguments)
        if not isinstance(parsed_arguments, dict):
            raise ProviderError("Provider tool arguments must be a JSON object.")
        seen_ids.add(call_id)
        calls.append(ToolCall(id=call_id, name=name, arguments=parsed_arguments))
    if not content and not calls:
        raise ProviderError("Provider returned an empty completion.")
    usage = data.get("usage")
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise ProviderError("Provider returned invalid token usage.")
    return Completion(
        content=content,
        tool_calls=calls,
        input_tokens=_token_count(usage, "prompt_tokens"),
        output_tokens=_token_count(usage, "completion_tokens"),
    )


class OpenAICompatibleProvider:
    """Use a caller-selected Chat Completions model, with bounded retry and I/O.

    ``base_url`` includes any API prefix (for example ``https://host.example/v1``).
    An empty API key omits Authorization, supporting local development servers.
    Redirects are disabled so a redirected request cannot forward credentials.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 45,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(base_url, str):
            raise ProviderError("Provider base URL must be an HTTP(S) API URL.")
        try:
            parsed_url = urlsplit(base_url)
            valid_url = (
                parsed_url.scheme in {"http", "https"}
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not parsed_url.query
                and not parsed_url.fragment
            )
            # Accessing port also validates its range and syntax.
            _ = parsed_url.port
            httpx.URL(base_url)
        except (TypeError, ValueError, httpx.InvalidURL):
            valid_url = False
        if not valid_url:
            raise ProviderError("Provider base URL must be an HTTP(S) API URL.")
        if not isinstance(api_key, str) or any(not 33 <= ord(c) <= 126 for c in api_key):
            raise ProviderError("Provider API key is invalid.")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ProviderError("A valid Chat Completions model must be configured.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
        ):
            raise ProviderError("Provider timeout must be greater than 0 and at most 300 seconds.")
        if type(max_retries) is not int or not 0 <= max_retries <= MAX_RETRIES:
            raise ProviderError("Provider max_retries must be an integer between 0 and 5.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.transport = transport

    def _payload(self, messages: list[dict], tools: list[dict] | None) -> bytes:
        if (
            not isinstance(messages, list)
            or not 1 <= len(messages) <= MAX_MESSAGES
            or any(not isinstance(message, dict) for message in messages)
        ):
            raise ProviderError("Provider requires 1 to 256 message objects.")
        if tools is not None and (
            not isinstance(tools, list)
            or len(tools) > MAX_TOOL_CALLS
            or any(not isinstance(tool, dict) for tool in tools)
        ):
            raise ProviderError("Provider accepts at most 32 tool definitions.")
        payload: dict = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ProviderError("Provider request is not valid JSON.") from None
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ProviderError("Provider request exceeded the size limit.")
        return encoded

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> Completion:
        payload = self._payload(messages, tools)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport, follow_redirects=False
        ) as client:
            for attempt in range(self.max_retries + 1):
                retry_after: str | None = None
                failure = "Provider request failed."
                try:
                    # The overall deadline bounds even a server that drips bytes forever.
                    async with asyncio.timeout(self.timeout):
                        async with client.stream(
                            "POST",
                            f"{self.base_url}/chat/completions",
                            content=payload,
                            headers=headers,
                        ) as response:
                            status = response.status_code
                            if status == 429 or 500 <= status < 600:
                                failure = f"Provider request failed with HTTP {status}."
                                retry_after = response.headers.get("Retry-After")
                            elif not 200 <= status < 300:
                                raise ProviderError(f"Provider request failed with HTTP {status}.")
                            else:
                                chunks = bytearray()
                                async for chunk in response.aiter_bytes():
                                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                                        raise ProviderError(
                                            "Provider response exceeded the size limit."
                                        )
                                    chunks.extend(chunk)
                                return _parse_completion(bytes(chunks))
                except (httpx.TransportError, TimeoutError):
                    failure = "Provider request failed due to a connection error or timeout."
                except httpx.DecodingError:
                    raise ProviderError("Provider returned invalid response encoding.") from None
                if attempt >= self.max_retries:
                    raise ProviderError(failure) from None
                delay = min(0.25 * (2**attempt), MAX_BACKOFF_SECONDS)
                if retry_after:
                    try:
                        requested_delay = float(retry_after)
                        if math.isfinite(requested_delay):
                            delay = min(max(delay, requested_delay), MAX_BACKOFF_SECONDS)
                    except ValueError:
                        pass
                await asyncio.sleep(delay)
        # Defensive: retries always return or raise.
        raise ProviderError("Provider request failed.")
