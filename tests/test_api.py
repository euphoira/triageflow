import time

import pytest
from fastapi.testclient import TestClient

from triageflow.api import create_app
from triageflow.config import Settings
from triageflow.demo import CASES


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as test_client:
        yield test_client


def test_config_never_exposes_key_or_absolute_repo(client):
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["mode"] == "demo"
    assert "api_key" not in response.text
    assert "base_url" not in response.text


def test_end_to_end_api_report_and_event_history(client):
    response = client.post(
        "/api/runs", json={"project_id": "demo-shop", "issue": CASES[0]["issue"]}
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert run["status"] == "completed", run
    assert run["report"]["verified"]
    events = client.get(f"/api/runs/{run_id}/events").json()
    assert len({e["seq"] for e in events}) == len(events)
    assert any(e["type"] == "tool_call" for e in events)
    report = client.get(f"/api/runs/{run_id}/report")
    assert report.status_code == 200
    assert "attachment" in report.headers["content-disposition"]
    assert "checkout.py" in report.text
    assert client.post(f"/api/runs/{run_id}/resume").status_code == 409
    assert client.get("/api/runs").json()[0]["id"] == run_id


@pytest.mark.parametrize(
    "payload",
    [
        {"project_id": "../../", "issue": "Please inspect this repository"},
        {"project_id": "demo-shop", "issue": "short"},
        {"project_id": "demo-shop", "issue": " " * 20},
        {"project_id": "demo-shop", "issue": "hello world", "repo": "C:/"},
    ],
)
def test_rejects_invalid_inputs(client, payload):
    assert client.post("/api/runs", json=payload).status_code == 422


def test_unknown_run_and_unready_report(client):
    assert client.get("/api/runs/absent").status_code == 404
    assert client.get("/api/runs/absent/events").status_code == 404
    run = client.app.state.store.create("demo-shop", "not started yet", "demo")
    assert client.get(f"/api/runs/{run.id}/report").status_code == 409


def test_cross_origin_and_host_protection(client):
    body = {"project_id": "demo-shop", "issue": CASES[0]["issue"]}
    assert (
        client.post("/api/runs", json=body, headers={"Origin": "https://other.example"}).status_code
        == 403
    )
    assert client.get("/health", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/health").headers["x-content-type-options"] == "nosniff"


def test_oversized_body_rejected(client):
    assert client.post("/api/runs", content="x" * 33_000).status_code == 413
