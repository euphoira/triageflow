import asyncio
import json

import pytest

from triageflow.config import Settings
from triageflow.demo import CASES
from triageflow.engine import Engine, markdown_report
from triageflow.evaluation import evaluate
from triageflow.providers import Completion, ProviderError, ToolCall
from triageflow.schemas import Report
from triageflow.store import Store


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path)
    return settings, Store(tmp_path / "test.sqlite3")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
async def test_demo_uses_real_tools_and_exact_evidence(setup, case):
    settings, store = setup
    run = store.create("demo-shop", case["issue"], "demo")
    result = await Engine(settings, store).execute(run.id)
    assert result.status == "completed", result.error
    assert result.report.verified
    assert result.report.evidence[0].path == case["path"]
    assert result.metrics.tool_calls == 3
    assert result.metrics.model_calls == 0
    assert [e["stage"] for e in store.events(run.id) if e["type"] == "stage_started"] == [
        "planner",
        "investigator",
        "reviewer",
    ]
    assert "尚未执行" in markdown_report(result)


async def test_unknown_issue_abstains(setup):
    settings, store = setup
    run = store.create("demo-shop", "The logo looks wrong in the browser", "demo")
    result = await Engine(settings, store).execute(run.id)
    assert result.status == "completed"
    assert not result.report.verified
    assert result.report.evidence == []
    assert result.report.confidence == "low"


async def test_completed_run_is_not_reexecuted(setup):
    settings, store = setup
    run = store.create("demo-shop", CASES[0]["issue"], "demo")
    engine = Engine(settings, store)
    first = await engine.execute(run.id)
    second = await engine.execute(run.id)
    assert first == second
    with pytest.raises(ValueError):
        store.requeue(run.id)


def test_restart_recovery_and_single_claim(setup):
    _, store = setup
    run = store.create("demo-shop", CASES[0]["issue"], "demo")
    assert store.claim(run.id) is not None
    assert store.claim(run.id) is None
    store.recover()
    assert store.load(run.id)[0].status == "interrupted"
    store.requeue(run.id)
    assert store.claim(run.id) is not None


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    async def complete(self, messages, tools=None):
        self.messages.append(json.loads(json.dumps(messages)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def live_setup(tmp_path, responses, **kwargs):
    settings = Settings(
        mode="live", model="test-model", api_key="test-only", data_dir=tmp_path, **kwargs
    )
    store = Store(tmp_path / "live.sqlite3")
    provider = ScriptedProvider(responses)
    engine = Engine(settings, store, provider)
    run = store.create("demo-shop", CASES[0]["issue"], "live")
    return engine, store, run, provider


def completion(content=None, calls=None):
    return Completion(content, calls or [], input_tokens=3, output_tokens=2)


def draft(**changes):
    return {
        "summary": "Insufficient evidence",
        "root_cause": "Unconfirmed",
        "confidence": "low",
        "evidence": [],
        "recommendations": [],
        "test_plan": [],
        "limitations": [],
        "verified": False,
        **changes,
    }


async def test_live_tool_protocol_and_reviewer(tmp_path):
    from triageflow.config import SAMPLE_ROOT
    from triageflow.repository import Repository

    read = Repository(SAMPLE_ROOT).read_file("checkout.py", 4, 8)
    report = draft(
        evidence=[
            {"path": "checkout.py", "start_line": 4, "end_line": 8, "quote": read["content"]}
        ],
        confidence="medium",
    )
    responses = [
        completion('{"steps":["inspect source"]}'),
        completion(calls=[ToolCall("read", "read_file", {"path": "checkout.py"})]),
        completion(calls=[ToolCall("finish", "submit_report", report)]),
        completion('{"accepted": true, "feedback": "supported"}'),
    ]
    engine, store, run, provider = live_setup(tmp_path, responses)
    result = await engine.execute(run.id)
    assert result.status == "completed", result.error
    assert result.report.verified
    assert result.metrics.model_calls == 4
    assert result.metrics.input_tokens == 12
    assert provider.messages[2][-1]["role"] == "tool"
    assert provider.messages[2][-1]["tool_call_id"] == "read"


async def test_fabricated_citation_cannot_be_published(tmp_path):
    forged = draft(
        evidence=[
            {"path": "checkout.py", "start_line": 1, "end_line": 1, "quote": "invented source"}
        ]
    )
    engine, store, run, _ = live_setup(
        tmp_path,
        [
            completion('{"steps":["inspect"]}'),
            completion(calls=[ToolCall("finish", "submit_report", forged)]),
        ],
    )
    result = await engine.execute(run.id)
    assert result.status == "failed"
    assert result.report is None
    assert "核验" in result.error


async def test_real_but_unread_citation_is_rejected(tmp_path):
    from triageflow.config import SAMPLE_ROOT

    quote = (SAMPLE_ROOT / "checkout.py").read_text().splitlines()[0]
    report = draft(
        evidence=[{"path": "checkout.py", "start_line": 1, "end_line": 1, "quote": quote}]
    )
    engine, _, run, _ = live_setup(
        tmp_path,
        [
            completion('{"steps":["inspect"]}'),
            completion(calls=[ToolCall("finish", "submit_report", report)]),
        ],
    )
    assert (await engine.execute(run.id)).status == "failed"


async def test_failure_resume_skips_planner_and_preserves_budget(tmp_path):
    engine, store, run, provider = live_setup(
        tmp_path,
        [
            completion('{"steps":["inspect"]}'),
            ProviderError("Temporary provider failure"),
            completion(calls=[ToolCall("finish", "submit_report", draft())]),
            completion('{"accepted":true,"feedback":"honest abstention"}'),
        ],
    )
    failed = await engine.execute(run.id)
    assert failed.status == "failed"
    assert store.load(run.id)[1]["next"] == "investigator"
    store.requeue(run.id)
    completed = await engine.execute(run.id)
    assert completed.status == "completed", completed.error
    assert completed.metrics.model_calls == 4
    assert len(provider.messages) == 4
    assert (
        sum(e["stage"] == "planner" for e in store.events(run.id) if e["type"] == "stage_started")
        == 1
    )


async def test_budget_cannot_be_reset_by_resume(tmp_path):
    engine, store, run, _ = live_setup(
        tmp_path, [completion('{"steps":["inspect"]}')], max_model_calls=1
    )
    assert (await engine.execute(run.id)).status == "failed"
    store.requeue(run.id)
    result = await engine.execute(run.id)
    assert result.status == "failed"
    assert result.metrics.model_calls == 1


async def test_reviewer_rejection_downgrades_conclusion(tmp_path):
    engine, _, run, _ = live_setup(
        tmp_path,
        [
            completion('{"steps":["inspect"]}'),
            completion(calls=[ToolCall("finish", "submit_report", draft(confidence="high"))]),
            completion('{"accepted":false,"feedback":"unsupported root cause"}'),
        ],
    )
    result = await engine.execute(run.id)
    assert result.report.confidence == "low"
    assert any("unsupported" in text for text in result.report.limitations)


async def test_cancel_records_interrupted_checkpoint(tmp_path):
    engine, store, run, _ = live_setup(tmp_path, [asyncio.CancelledError()])

    # CancelledError derives from BaseException, so use a purpose-built provider.
    class Cancel:
        async def complete(self, *args):
            raise asyncio.CancelledError()

    engine.provider = Cancel()
    with pytest.raises(asyncio.CancelledError):
        await engine.execute(run.id)
    assert store.load(run.id)[0].status == "interrupted"


async def test_synthetic_regression_results_are_labeled(setup):
    settings, _ = setup
    result = await evaluate(settings)
    assert result["passed"] == result["total"] == 8
    assert "NOT" in result["scope"]
    assert result["mode"] == "demo"


def test_export_fences_untrusted_backticks(setup):
    _, store = setup
    run = store.create("demo-shop", "sample issue with ten chars", "demo")
    run.report = Report.model_validate(
        draft(
            evidence=[
                {"path": "example.md", "start_line": 1, "end_line": 1, "quote": "```untrusted"}
            ]
        )
    )
    assert "````text\n```untrusted\n````" in markdown_report(run)
