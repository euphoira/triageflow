"""Regression tests for durable resumes and evidence at workflow boundaries."""

import asyncio
import copy
import json

import pytest

from triageflow.config import Settings
from triageflow.engine import Engine
from triageflow.providers import Completion, ToolCall
from triageflow.store import Store


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    async def complete(self, messages, tools=None):
        self.messages.append(copy.deepcopy(messages))
        assert_complete_tool_groups(messages)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response()
        return response


def assert_complete_tool_groups(messages):
    """A provider must never receive an assistant with unanswered tool calls."""
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
            pending = {call["id"] for call in message.get("tool_calls", [])}
    assert not pending


def completion(content=None, calls=None):
    return Completion(content, calls or [], input_tokens=3, output_tokens=2)


def draft(**changes):
    return {
        "summary": "A suspected source defect",
        "root_cause": "A branch needs investigation",
        "confidence": "medium",
        "evidence": [],
        "recommendations": [],
        "test_plan": [],
        "limitations": [],
        "verified": False,
        **changes,
    }


def evidence(quote="value = False", end_line=1):
    return {"path": "main.py", "start_line": 1, "end_line": end_line, "quote": quote}


def local_workflow(tmp_path, responses, source="value = False\n"):
    root = tmp_path / "source"
    root.mkdir()
    (root / "main.py").write_text(source, encoding="utf-8")
    settings = Settings(
        mode="live",
        model="test-model",
        api_key="test-only",
        repo=root,
        data_dir=tmp_path,
    )
    store = Store(tmp_path / "workflow.sqlite3")
    provider = ScriptedProvider(responses)
    run = store.create("local", "The returned value is incorrect for this input.", "live")
    return Engine(settings, store, provider), store, run, provider


@pytest.mark.parametrize(
    ("source", "quote", "end_line"),
    [
        ("value = False\n\n", "value = False\n", 2),
        ('label = "a\u2028b"\n', 'label = "a\u2028b"', 1),
        ("\n\n", "\n", 2),
    ],
    ids=["trailing-blank-line", "unicode-separator-is-not-a-newline", "blank-lines"],
)
async def test_exact_citation_preserves_source_line_boundaries(tmp_path, source, quote, end_line):
    report = draft(evidence=[evidence(quote, end_line)])
    engine, store, run, _ = local_workflow(
        tmp_path,
        [
            completion('{"steps":["inspect source"]}'),
            completion(calls=[ToolCall("read", "read_file", {"path": "main.py"})]),
            completion(calls=[ToolCall("submit", "submit_report", report)]),
            completion('{"accepted":true,"feedback":"Exact source excerpt"}'),
        ],
        source,
    )

    result = await engine.execute(run.id)

    assert result.status == "completed", result.error
    assert result.report.verified
    assert result.report.evidence[0].quote == quote
    assert store.load(run.id)[0].report.evidence[0].quote == quote
    citation_events = [
        event
        for event in store.events(run.id)
        if event["type"] == "verification" and "path" in event["data"]
    ]
    assert citation_events[0]["data"]["valid"] is True


@pytest.mark.parametrize("accepted", [False, True], ids=["rejected", "accepted"])
@pytest.mark.parametrize("has_evidence", [False, True], ids=["abstention", "cited"])
async def test_full_limitations_list_remains_valid_after_review(tmp_path, accepted, has_evidence):
    report = draft(
        confidence="high",
        evidence=[evidence()] if has_evidence else [],
        limitations=[f"Model limitation {index}" for index in range(12)],
    )
    responses = [completion('{"steps":["inspect source"]}')]
    if has_evidence:
        responses.append(
            completion(
                calls=[
                    ToolCall("read", "read_file", {"path": "main.py"}),
                ]
            )
        )
    responses.extend(
        [
            completion(calls=[ToolCall("submit", "submit_report", report)]),
            completion(json.dumps({"accepted": accepted, "feedback": "review-boundary-feedback"})),
        ]
    )
    engine, store, run, _ = local_workflow(tmp_path, responses)

    result = await engine.execute(run.id)

    assert result.status == "completed", result.error
    assert len(result.report.limitations) <= 12
    assert result.report.limitations[0] == "Model limitation 0"
    if not accepted:
        assert any("review-boundary-feedback" in text for text in result.report.limitations)
    if not has_evidence:
        assert any("未获得可验证" in text for text in result.report.limitations)
    if not accepted or not has_evidence:
        assert result.report.confidence == "low"
    # Save can serialize a mutated Pydantic model without revalidating it. Reloading is essential.
    restarted = Store(store.path)
    assert restarted.load(run.id)[0] == result
    assert restarted.list() == [result]


async def test_resume_rejects_mode_change_without_consuming_more_budget(tmp_path):
    engine, store, run, provider = local_workflow(
        tmp_path,
        [
            completion('{"steps":["inspect source"]}'),
            asyncio.CancelledError(),
        ],
    )
    with pytest.raises(asyncio.CancelledError):
        await engine.execute(run.id)
    interrupted, original_checkpoint = store.load(run.id)
    assert interrupted.status == "interrupted"
    assert original_checkpoint["next"] == "investigator"
    assert interrupted.metrics.model_calls == 2

    restarted = Store(store.path)
    restarted.requeue(run.id)
    demo_settings = Settings(mode="demo", repo=engine.settings.repo, data_dir=tmp_path)
    result = await Engine(demo_settings, restarted, provider).execute(run.id)

    assert result.status == "failed"
    assert "模式" in result.error
    assert result.report is None
    assert result.metrics.model_calls == interrupted.metrics.model_calls
    assert result.metrics.tool_calls == interrupted.metrics.tool_calls
    assert len(provider.messages) == 2
    assert restarted.load(run.id)[1] == original_checkpoint


async def test_changed_source_cannot_publish_previously_inspected_quote(tmp_path):
    report = draft(evidence=[evidence()])
    source_path = tmp_path / "source" / "main.py"

    def change_source_then_submit():
        source_path.write_text("value = True\n", encoding="utf-8")
        return completion(calls=[ToolCall("submit", "submit_report", report)])

    engine, store, run, provider = local_workflow(
        tmp_path,
        [
            completion('{"steps":["inspect source"]}'),
            completion(calls=[ToolCall("read", "read_file", {"path": "main.py"})]),
            change_source_then_submit,
        ],
    )

    result = await engine.execute(run.id)

    assert result.status == "failed"
    assert result.report is None
    assert "核验" in result.error
    assert len(provider.messages) == 3  # Reviewer is not called with invalid source provenance.
    saved, checkpoint = Store(store.path).load(run.id)
    assert saved.status == "failed"
    assert checkpoint["next"] == "reviewer"
    assert checkpoint["reads"][0]["content"] == "value = False"
    failed_checks = [event for event in store.events(run.id) if event["type"] == "verification"]
    assert failed_checks[-1]["data"]["valid"] is False
    assert not any(event["type"] == "completed" for event in store.events(run.id))


async def test_partial_tool_batch_checkpoint_resumes_with_only_complete_messages(
    tmp_path,
    monkeypatch,
):
    batch = [
        ToolCall("read-partial", "read_file", {"path": "main.py"}),
        ToolCall("search-unstarted", "search", {"query": "False"}),
    ]
    engine, store, run, _ = local_workflow(
        tmp_path,
        [
            completion('{"steps":["inspect source"]}'),
            completion(calls=[ToolCall("list-complete", "list_files", {})]),
            completion(calls=batch),
        ],
    )
    real_repository_call = engine.repository_call
    durable_snapshots = []

    def interrupt_after_first_read(*args, **kwargs):
        result = real_repository_call(*args, **kwargs)
        if args[3] == "read_file":
            # Inspect SQLite before execute() gets to its cancellation handler/final save.
            durable_snapshots.append(Store(store.path).load(run.id))
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(engine, "repository_call", interrupt_after_first_read)
    with pytest.raises(asyncio.CancelledError):
        await engine.execute(run.id)

    before_cancel, durable_checkpoint = durable_snapshots[0]
    assert before_cancel.metrics.model_calls == 3
    assert before_cancel.metrics.tool_calls == 2
    assert durable_checkpoint["rounds"] == 2
    assert durable_checkpoint["reads"][0]["content"] == "value = False"
    assert_complete_tool_groups(durable_checkpoint["messages"])
    assert [message["role"] for message in durable_checkpoint["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    assert durable_checkpoint["messages"][-1]["tool_call_id"] == "list-complete"
    assert "read-partial" not in json.dumps(durable_checkpoint["messages"])
    assert "search-unstarted" not in json.dumps(durable_checkpoint["messages"])

    restarted = Store(store.path)
    interrupted, checkpoint = restarted.load(run.id)
    assert interrupted.status == "interrupted"
    assert checkpoint == durable_checkpoint
    restarted.requeue(run.id)
    resumed_provider = ScriptedProvider(
        [
            completion(
                calls=[
                    ToolCall("read-resumed", "read_file", {"path": "main.py"}),
                    ToolCall("search-resumed", "search", {"query": "False"}),
                ]
            ),
            completion(calls=[ToolCall("submit", "submit_report", draft(evidence=[evidence()]))]),
            completion('{"accepted":true,"feedback":"Source-supported investigation"}'),
        ]
    )
    resumed_engine = Engine(engine.settings, restarted, resumed_provider)
    result = await resumed_engine.execute(run.id)

    assert result.status == "completed", result.error
    assert result.report.verified
    assert resumed_provider.messages[0] == durable_checkpoint["messages"]
    assert result.metrics.model_calls == 6
    assert result.metrics.tool_calls == 4
    assert result.metrics.input_tokens == 18
    assert result.metrics.output_tokens == 12
    assert restarted.load(run.id)[1]["next"] == "completed"
    assert (
        sum(
            event["type"] == "stage_started" and event["stage"] == "planner"
            for event in restarted.events(run.id)
        )
        == 1
    )
