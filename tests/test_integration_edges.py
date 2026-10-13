"""Integration boundaries between HTTP configuration, the tool loop, and the CLI."""

import copy
import json
import sys

import pytest
from fastapi.testclient import TestClient

from triageflow import cli
from triageflow.api import create_app
from triageflow.config import Settings
from triageflow.demo import CASES
from triageflow.engine import Engine
from triageflow.providers import Completion, ToolCall
from triageflow.store import Store


@pytest.fixture
def edge_client(tmp_path):
    with TestClient(
        create_app(Settings(data_dir=tmp_path)), raise_server_exceptions=False
    ) as client:
        yield client


@pytest.mark.parametrize("origin", ["http://[", "http://[invalid]", "null"])
def test_malformed_origins_fail_closed_without_server_error(edge_client, origin):
    response = edge_client.post(
        "/api/runs",
        json={"project_id": "demo-shop", "issue": CASES[0]["issue"]},
        headers={"Origin": origin},
    )
    assert response.status_code in {400, 403}
    assert edge_client.get("/api/runs").json() == []


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_body_limit_covers_chunked_and_misreported_length(edge_client, declared_length):
    body = b" " * 33_000 + json.dumps({"issue": CASES[0]["issue"]}).encode()
    headers = {"Content-Type": "application/json"}
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    response = edge_client.post("/api/runs", content=iter([body]), headers=headers)
    assert response.status_code == 413
    assert edge_client.get("/api/runs").json() == []


def test_same_origin_write_remains_usable(edge_client):
    response = edge_client.post(
        "/api/runs",
        json={"issue": CASES[0]["issue"]},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 202


def test_malformed_host_on_write_is_rejected_before_origin_parsing(edge_client):
    response = edge_client.post(
        "/api/runs", json={"issue": CASES[0]["issue"]}, headers={"Host": "["}
    )
    assert response.status_code in {400, 403}


def test_demo_config_does_not_claim_a_configured_live_model_is_running(tmp_path):
    settings = Settings(mode="demo", model="unused-live-model", data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        config = client.get("/api/config").json()
    assert config["mode"] == "demo"
    assert config["model"] == "deterministic-fixtures"


def test_live_config_reports_selected_model_without_credentials(tmp_path):
    settings = Settings(
        mode="live", model="selected-model", api_key="private-test-key", data_dir=tmp_path
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/config")
    assert response.json()["model"] == "selected-model"
    assert "private-test-key" not in response.text


def test_cli_provider_configuration_error_exits_cleanly(tmp_path, monkeypatch, capsys):
    for key, value in {
        "TRIAGEFLOW_MODE": "live",
        "TRIAGEFLOW_MODEL": "selected-model",
        "TRIAGEFLOW_API_KEY": "private-test-key",
        "TRIAGEFLOW_BASE_URL": "file:///invalid-endpoint",
        "TRIAGEFLOW_DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("TRIAGEFLOW_REPO", raising=False)
    monkeypatch.setattr(cli, "load_dotenv", lambda: None)
    monkeypatch.setattr(sys, "argv", ["triageflow", "run", "Investigate this checkout issue"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    stderr = capsys.readouterr().err
    assert "triageflow:" in stderr
    assert "private-test-key" not in stderr


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    async def complete(self, messages, tools=None):
        self.messages.append(copy.deepcopy(messages))
        return next(self.responses)


def abstention():
    return {
        "summary": "Evidence is insufficient.",
        "root_cause": "Unconfirmed.",
        "confidence": "low",
        "evidence": [],
        "recommendations": [],
        "test_plan": [],
        "limitations": [],
    }


def live_engine(tmp_path, responses, **limits):
    settings = Settings(
        mode="live", model="test-model", api_key="test-only", data_dir=tmp_path, **limits
    )
    store = Store(tmp_path / "edges.sqlite3")
    provider = ScriptedProvider(responses)
    engine = Engine(settings, store, provider)
    run = store.create("demo-shop", CASES[0]["issue"], "live")
    return engine, store, run, provider


async def test_unknown_tool_returns_protocol_valid_error_to_model(tmp_path):
    engine, _, run, provider = live_engine(
        tmp_path,
        [
            Completion('{"steps":["inspect source"]}'),
            Completion(None, [ToolCall("bad", "run_shell", {"command": "never execute"})]),
            Completion(None, [ToolCall("done", "submit_report", abstention())]),
            Completion('{"accepted":true,"feedback":"honest abstention"}'),
        ],
    )
    result = await engine.execute(run.id)
    assert result.status == "completed", result.error
    tool_reply = provider.messages[2][-1]
    assert tool_reply["role"] == "tool"
    assert tool_reply["tool_call_id"] == "bad"
    assert "error" in json.loads(tool_reply["content"])
    assert result.metrics.tool_calls == 1


async def test_mixed_report_and_repository_call_is_rejected_before_read(tmp_path):
    engine, store, run, _ = live_engine(
        tmp_path,
        [
            Completion('{"steps":["inspect source"]}'),
            Completion(
                None,
                [
                    ToolCall("read", "read_file", {"path": "checkout.py"}),
                    ToolCall("done", "submit_report", abstention()),
                ],
            ),
        ],
    )
    result = await engine.execute(run.id)
    assert result.status == "failed"
    assert "submit_report" in result.error
    assert result.metrics.tool_calls == 0
    assert not any(event["type"] == "tool_call" for event in store.events(run.id))


async def test_partial_tool_group_never_leaves_invalid_messages_on_resume(tmp_path):
    engine, store, run, provider = live_engine(
        tmp_path,
        [
            Completion('{"steps":["inspect source"]}'),
            Completion(
                None,
                [
                    ToolCall("first", "list_files", {}),
                    ToolCall("second", "list_files", {}),
                ],
            ),
            Completion(None, [ToolCall("done", "submit_report", abstention())]),
            Completion('{"accepted":true,"feedback":"honest abstention"}'),
        ],
        max_tool_calls=1,
    )
    failed = await engine.execute(run.id)
    assert failed.status == "failed"
    assert failed.metrics.tool_calls == 1
    checkpoint = store.load(run.id)[1]
    assert not any(message["role"] == "assistant" for message in checkpoint["messages"])
    store.requeue(run.id)
    result = await engine.execute(run.id)
    assert result.status == "completed", result.error
    assert result.metrics.tool_calls == 1
    assert provider.messages[2] == checkpoint["messages"]


async def test_investigation_round_budget_cannot_be_reset_by_resume(tmp_path):
    engine, store, run, provider = live_engine(
        tmp_path,
        [
            Completion('{"steps":["inspect source"]}'),
            Completion(None, [ToolCall("list", "list_files", {})]),
        ],
        max_steps=1,
    )
    failed = await engine.execute(run.id)
    assert failed.status == "failed"
    assert store.load(run.id)[1]["rounds"] == 1
    store.requeue(run.id)
    failed = await engine.execute(run.id)
    assert failed.status == "failed"
    assert failed.metrics.model_calls == 2
    assert len(provider.messages) == 2
