"""Transport-level regression tests; no network access or credentials are needed."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from triageflow import providers
from triageflow.providers import Completion, OpenAICompatibleProvider, ProviderError, ToolCall

MESSAGES = [{"role": "user", "content": "Find the bug."}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Find text in the repository.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }
]


def completion_payload(content="A useful answer", tool_calls=None, **extra):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        **extra,
    }


def tool_payload(arguments='{"query":"cache"}'):
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": "search", "arguments": arguments},
    }


def provider(handler, **kwargs):
    return OpenAICompatibleProvider(
        "https://provider.example/v1/",
        "test-secret",
        "configured-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_function_call_request_response_and_token_usage():
    def handler(request):
        assert str(request.url) == "https://provider.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload == {
            "model": "configured-model",
            "messages": MESSAGES,
            "stream": False,
            "tools": TOOLS,
            "tool_choice": "auto",
        }
        return httpx.Response(
            200,
            json=completion_payload(
                None,
                [tool_payload()],
                usage={"prompt_tokens": 21, "completion_tokens": 12},
            ),
        )

    result = await provider(handler).complete(MESSAGES, TOOLS)
    assert result == Completion(None, [ToolCall("call_1", "search", {"query": "cache"})], 21, 12)


@pytest.mark.asyncio
async def test_text_completion_and_empty_key_for_local_server():
    def handler(request):
        assert "authorization" not in request.headers
        assert "tools" not in json.loads(request.content)
        return httpx.Response(200, json=completion_payload("已经定位错误。"))

    client = OpenAICompatibleProvider(
        "http://localhost:11434/v1", "", "local-model", transport=httpx.MockTransport(handler)
    )
    assert await client.complete(MESSAGES, []) == Completion("已经定位错误。", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503, 599])
async def test_retries_only_transient_statuses_with_bounded_delay(monkeypatch, status):
    attempts = []
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(providers.asyncio, "sleep", fake_sleep)

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return httpx.Response(status, headers={"Retry-After": "3600"}, text="private error")
        return httpx.Response(200, json=completion_payload())

    result = await provider(handler).complete(MESSAGES)
    assert result.content == "A useful answer"
    assert len(attempts) == 3
    assert delays == [providers.MAX_BACKOFF_SECONDS] * 2


@pytest.mark.asyncio
async def test_retry_exhaustion_is_bounded_and_sanitized(monkeypatch):
    attempts = []

    async def fake_sleep(_delay):
        pass

    monkeypatch.setattr(providers.asyncio, "sleep", fake_sleep)

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("private url and test-secret", request=request)

    with pytest.raises(ProviderError) as error:
        await provider(handler, max_retries=2).complete(MESSAGES)
    assert len(attempts) == 3
    assert "test-secret" not in str(error.value)
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 422])
async def test_non_transient_errors_never_retry_or_expose_body(status):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(
            status,
            headers={"Location": "https://other.example/steal"},
            text="test-secret and private server stacktrace",
        )

    with pytest.raises(ProviderError, match=f"HTTP {status}") as error:
        await provider(handler).complete(MESSAGES)
    assert len(attempts) == 1
    assert "test-secret" not in str(error.value)
    assert "stacktrace" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not JSON containing test-secret",
        b"[]",
        b'{"choices":[]}',
        b'{"choices":[{"message":null}]}',
        json.dumps(completion_payload(content=[{"text": "bad"}])).encode(),
        json.dumps(completion_payload(content=None)).encode(),
        json.dumps(completion_payload(tool_calls="bad")).encode(),
        json.dumps(completion_payload(tool_calls=[tool_payload("[]")])).encode(),
        json.dumps(completion_payload(tool_calls=[tool_payload("{broken")])).encode(),
        json.dumps(completion_payload(tool_calls=[tool_payload('{"x":NaN}')])).encode(),
        json.dumps(completion_payload(tool_calls=[tool_payload({"query": "x"})])).encode(),
        json.dumps(completion_payload(tool_calls=[tool_payload(), tool_payload()])).encode(),
        json.dumps(completion_payload(usage={"prompt_tokens": -1})).encode(),
        json.dumps(completion_payload(usage={"completion_tokens": True})).encode(),
        json.dumps(completion_payload(usage=["invalid"])).encode(),
        json.dumps(completion_payload(usage=[])).encode(),
        json.dumps(completion_payload(usage=False)).encode(),
    ],
)
async def test_malformed_completions_raise_sanitized_error_without_retry(body):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(200, content=body)

    with pytest.raises(ProviderError) as error:
        await provider(handler).complete(MESSAGES)
    assert len(attempts) == 1
    assert "test-secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
async def test_incomplete_or_filtered_output_fails(finish_reason):
    payload = completion_payload()
    payload["choices"][0]["finish_reason"] = finish_reason
    with pytest.raises(ProviderError):
        await provider(lambda _: httpx.Response(200, json=payload)).complete(MESSAGES)


@pytest.mark.asyncio
async def test_response_limit_is_enforced_while_streaming_and_stream_is_closed(monkeypatch):
    monkeypatch.setattr(providers, "MAX_RESPONSE_BYTES", 32)

    class ResponseStream(httpx.AsyncByteStream):
        closed = False
        reads = 0

        async def __aiter__(self):
            for _ in range(10):
                self.reads += 1
                yield b"x" * 20

        async def aclose(self):
            self.closed = True

    stream = ResponseStream()
    with pytest.raises(ProviderError, match="response exceeded"):
        await provider(lambda _: httpx.Response(200, stream=stream)).complete(MESSAGES)
    assert stream.reads == 2
    assert stream.closed


@pytest.mark.asyncio
async def test_overall_timeout_bounds_dripping_response():
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield b"data"

    with pytest.raises(ProviderError, match="timeout"):
        await provider(
            lambda _: httpx.Response(200, stream=SlowStream()), timeout=0.005, max_retries=0
        ).complete(MESSAGES)


@pytest.mark.asyncio
async def test_cancelled_requests_are_not_retried():
    attempts = []

    def handler(request):
        attempts.append(request)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await provider(handler).complete(MESSAGES)
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_request_limit_rejects_before_network(monkeypatch):
    monkeypatch.setattr(providers, "MAX_REQUEST_BYTES", 32)

    def unexpected_request(_):
        pytest.fail("oversized requests must not reach the server")

    with pytest.raises(ProviderError, match="request exceeded"):
        await provider(unexpected_request).complete(MESSAGES)


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [[], ["invalid"], MESSAGES * 257, [{"value": float("nan")}]])
async def test_invalid_messages_reject_before_network(messages):
    def unexpected_request(_):
        pytest.fail("invalid requests must not reach the server")

    with pytest.raises(ProviderError):
        await provider(unexpected_request).complete(messages)


@pytest.mark.asyncio
async def test_tool_argument_limit(monkeypatch):
    monkeypatch.setattr(providers, "MAX_ARGUMENT_BYTES", 5)
    payload = completion_payload(tool_calls=[tool_payload()])
    with pytest.raises(ProviderError, match="arguments exceeded"):
        await provider(lambda _: httpx.Response(200, json=payload)).complete(MESSAGES)


@pytest.mark.parametrize(
    "options",
    [
        {"base_url": "file:///private"},
        {"base_url": "https://user:secret@example.test"},
        {"base_url": "https://example.test?api_key=secret"},
        {"base_url": "https://example.test:invalid"},
        {"base_url": 123},
        {"base_url": "https://example.test/\x00"},
        {"api_key": "test-secret\r\ninjected: header"},
        {"api_key": "密钥"},
        {"model": ""},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"max_retries": -1},
        {"max_retries": 6},
        {"max_retries": True},
    ],
)
def test_invalid_configuration_is_rejected_without_leaking_secrets(options):
    config = {"base_url": "https://example.test/v1", "api_key": "test-secret", "model": "model"}
    config.update(options)
    with pytest.raises(ProviderError) as error:
        OpenAICompatibleProvider(**config)
    assert "test-secret" not in str(error.value)
