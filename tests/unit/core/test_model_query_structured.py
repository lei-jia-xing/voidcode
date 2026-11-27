"""Structured governed queries: validated data or a callable failure, never a pose."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.core_factory import loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.core.storage.store import SessionStatus, StagedBudgetUpdate, StoreConflict
from voidcode.sdk.hooks import UsageFact
from voidcode.sdk.provider import ProviderExecutionError
from voidcode.sdk.services import ModelQueryBudgetExhausted, ModelQueryPromptTooLong, ModelQueryToolCalls, PluginBudget

_SCHEMA = {
    "type": "object",
    "required": ["path", "line"],
    "properties": {"path": {"type": "string"}, "line": {"type": "integer"}},
    "additionalProperties": False,
}

_STRUCTURED_SUFFIX = "Reply with exactly one JSON object and nothing else."


def _turn(output: str | None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output}, finish="stop")


def _open(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    core = open_core(workspace, state_dir=tmp_path / "state")
    register_core(core)
    core.create_session("sess-structured")
    return core


def _request_messages(server: OpenAILoopbackServer, index: int) -> list[Mapping[str, object]]:
    raw = server.requests[index].get("messages")
    assert isinstance(raw, list)
    return [message for message in raw if isinstance(message, Mapping)]


def test_structured_extraction_returns_validated_data(tmp_path: Path) -> None:
    with loopback_server([_turn('{"path": "a.py", "line": 3}')]) as server:
        core = _open(tmp_path, server)
        result = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert result.data is not None and dict(result.data) == {"path": "a.py", "line": 3}
    assert result.parse_error is None
    assert result.raw_text == '{"path": "a.py", "line": 3}'
    assert result.done_reason == "stop"
    request = server.requests[0]
    assert not request.get("tools")
    messages = _request_messages(server, 0)
    assert len(messages) == 1 and str(messages[0].get("content", "")).endswith(_STRUCTURED_SUFFIX)
    facts = [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]
    assert facts and facts[0].payload["model"] == "exact-model" and "usage" in facts[0].payload


def test_unparseable_reply_is_callable_failure(tmp_path: Path) -> None:
    with loopback_server([_turn("I cannot answer that.")]) as server:
        core = _open(tmp_path, server)
        result = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert result.data is None
    assert result.parse_error is not None
    assert result.parse_error.kind == "invalid_json"
    assert result.parse_error.message
    assert result.raw_text == "I cannot answer that."
    assert [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]


def test_schema_violation_reports_location(tmp_path: Path) -> None:
    with loopback_server([_turn('{"path": "a.py"}')]) as server:
        core = _open(tmp_path, server)
        result = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert result.data is None
    assert result.parse_error is not None
    assert result.parse_error.kind == "schema_violation"
    assert result.parse_error.path == "$"
    assert "line" in result.parse_error.message


def test_non_object_and_empty_replies_never_pose(tmp_path: Path) -> None:
    with loopback_server([_turn("[1, 2]"), _turn(""), _turn(None)]) as server:
        core = _open(tmp_path, server)
        array = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
        empty = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
        missing = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert array.data is None and array.parse_error is not None and array.parse_error.kind == "schema_violation"
    assert empty.data is None and empty.parse_error is not None and empty.parse_error.kind == "invalid_json"
    assert missing.data is None and missing.parse_error is not None and missing.parse_error.kind == "invalid_json"
    assert len(server.requests) == 3


def test_provider_error_propagates_exactly_like_bare(tmp_path: Path) -> None:
    malformed = {"choices": []}
    with loopback_server([dict(malformed), dict(malformed)]) as server:
        core = _open(tmp_path, server)
        with pytest.raises(ProviderExecutionError):
            core.governed_model_query("sess-structured", "bare")
        with pytest.raises(ProviderExecutionError):
            core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA))
    assert len(server.requests) == 2


def test_batch_locates_the_failing_item(tmp_path: Path) -> None:
    with loopback_server([_turn('{"path": "a.py", "line": 1}'), _turn("nope"), _turn('{"path": "b.py", "line": 2}')]) as server:
        core = _open(tmp_path, server)
        results = [core.governed_model_query_structured("sess-structured", f"extract {index}", dict(_SCHEMA)) for index in range(3)]
    assert results[0].data is not None and results[2].data is not None
    assert results[1].data is None and results[1].parse_error is not None
    assert len(server.requests) == 3


def test_invalid_result_schema_fails_before_dispatch(tmp_path: Path) -> None:
    with loopback_server([_turn('{"path": "a.py", "line": 1}')]) as server:
        core = _open(tmp_path, server)
        with pytest.raises(ValueError, match="not a valid JSON schema"):
            core.governed_model_query_structured("sess-structured", "extract", {"type": 42})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="must be an object"):
            core.governed_model_query_structured("sess-structured", "extract", ["not", "an", "object"])  # type: ignore[arg-type]
    assert server.requests == []


def _usage_reply(output: str, *, usage: dict[str, object] | None | bool = True) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output}, finish="stop", usage=usage)


def _stage_budget(core: Core, session_id: str, **limits: int) -> None:
    core.store.commit_staged(
        session_id,
        run_id="run-budget",
        revision_id=core.revision_id,
        events=(),
        status=SessionStatus.IDLE,
        staged_budgets=(
            StagedBudgetUpdate(
                owner="find",
                action="set",
                budget=PluginBudget(owner="find", **limits),
            ),
        ),
    )


def test_query_usage_is_counted_once_and_owner_attributed(tmp_path: Path) -> None:
    reply = _usage_reply(
        '{"path": "a.py", "line": 3}',
        usage={"prompt_tokens": 120, "completion_tokens": 45, "prompt_tokens_details": {"cached_tokens": 30}},
    )
    with loopback_server([reply]) as server:
        core = _open(tmp_path, server)
        result = core.governed_model_query_structured("sess-structured", "extract", dict(_SCHEMA), owner="find")
        report = core.usage("sess-structured")
        events = [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]
    assert result.data is not None and dict(result.data) == {"path": "a.py", "line": 3}
    # The provider's one bill is reported exactly once, never doubled by another path.
    assert (report.input_tokens, report.output_tokens) == (120, 45)
    assert (report.cache_read_tokens, report.uncached_input_tokens) == (30, 90)
    assert report.turn_count == 1
    assert report.has_unknown_usage is False
    # The endpoint model has no catalog price: its unknown cost stays unknown.
    assert report.total_cost_usd is None
    assert len(events) == 1
    assert events[0].payload["owner"] == "find"
    assert events[0].payload["elapsed_ms"] >= 0


def test_query_missing_usage_stays_unknown(tmp_path: Path) -> None:
    with loopback_server([_usage_reply("plain answer", usage=None)]) as server:
        core = _open(tmp_path, server)
        assert core.governed_model_query("sess-structured", "bare", owner="btw") == "plain answer"
        report = core.usage("sess-structured")
        events = [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]
    assert report.input_tokens is None
    assert report.output_tokens is None
    assert report.cache_read_tokens is None
    assert report.uncached_input_tokens is None
    assert report.total_cost_usd is None
    assert report.has_unknown_usage is True
    assert report.turn_count == 1
    assert len(events) == 1
    assert events[0].payload["usage"] is None
    assert events[0].payload["cost_usd"] is None


def test_query_refuses_an_oversize_prompt_before_dispatch(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _open(tmp_path, server)
        with pytest.raises(ModelQueryPromptTooLong, match="64 KiB"):
            core.governed_model_query("sess-structured", "x" * (64 * 1024 + 1))
    assert server.requests == []


def test_query_refuses_an_exhausted_budget_before_dispatch(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _open(tmp_path, server)
        _stage_budget(core, "sess-structured", max_total_tokens=0)
        with pytest.raises(ModelQueryBudgetExhausted, match="budget limit exceeded: total tokens 0 >= 0 for owner find"):
            core.governed_model_query("sess-structured", "bare", owner="find")
        report = core.usage("sess-structured")
        events = [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]
    assert server.requests == []
    assert report.input_tokens == 0 and report.output_tokens == 0 and report.turn_count == 0
    assert events == []


def test_query_spend_counts_against_the_active_budget(tmp_path: Path) -> None:
    reply = _usage_reply("first answer", usage={"prompt_tokens": 120, "completion_tokens": 45})
    with loopback_server([reply]) as server:
        core = _open(tmp_path, server)
        _stage_budget(core, "sess-structured", max_total_tokens=150)
        assert core.governed_model_query("sess-structured", "first", owner="find") == "first answer"
        with pytest.raises(ModelQueryBudgetExhausted, match="budget limit exceeded: total tokens 165 >= 150 for owner find"):
            core.governed_model_query("sess-structured", "second", owner="find")
    assert len(server.requests) == 1


def test_query_fails_when_its_usage_cannot_be_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with loopback_server([_usage_reply("answer")]) as server:
        core = _open(tmp_path, server)
        original = core.store.commit_staged

        def fail_model_query_commit(session_id: str, **kwargs: object) -> object:
            events = kwargs.get("events", ())
            if any(event.event_type == "core.model_query" for event in events):  # type: ignore[attr-defined]
                raise StoreConflict("forced accounting failure")
            return original(session_id, **kwargs)

        monkeypatch.setattr(core.store, "commit_staged", fail_model_query_commit)
        with pytest.raises(StoreConflict, match="forced accounting failure"):
            core.governed_model_query("sess-structured", "bare", owner="find")
        events = [event for event in core.events_after("sess-structured") if event.event_type == "core.model_query"]
    assert len(server.requests) == 1
    assert events == []


def _command(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def _goal_state(core: Core, session_id: str) -> Mapping[str, object]:
    result = _command(core, session_id, "goal", {})
    assert result.status == "ok", result.error
    goal = result.data.get("goal")
    assert isinstance(goal, Mapping)
    return goal


def _all_facts(core: Core, session_id: str):
    cursor = 0
    facts = []
    while True:
        page = core.plugin_runtime.fact_page(session_id, cursor, 200)
        facts.extend(page.facts)
        cursor = page.next_sequence
        if not page.has_more:
            return facts


def test_goal_accounts_model_query_usage_once_and_closed_window_spend(tmp_path: Path) -> None:
    replies = [
        _usage_reply("goal setup turn", usage={"prompt_tokens": 100, "completion_tokens": 20}),
        _usage_reply("side answer", usage={"prompt_tokens": 40, "completion_tokens": 10}),
    ]
    with loopback_server(replies) as server:
        core = _open(tmp_path, server)
        core.auto_drain = False
        session = "sess-structured"
        assert core.run("start a goal session", session_id=session).status == "completed"
        created = _command(core, session, "goal.set", {"objective": "Ship the seam", "token_budget": 100})
        assert created.status == "ok", created.error
        assert core.governed_model_query(session, "check the seam", owner="btw") == "side answer"

        state = _goal_state(core, session)
        query_events = [event for event in core.events_after(session) if event.event_type == "core.model_query"]
        report = core.usage(session)
        facts = [fact for fact in _all_facts(core, session) if isinstance(fact, UsageFact) and fact.source == "model_query"]

        assert state["status"] == "active"
        assert state["tokens_used"] == 50
        assert state["window_tokens_used"] == 50
        assert state["window_allowance"] == 100
        assert len(query_events) == 1
        assert len(facts) == 1
        assert facts[0].event_sequence == facts[0].dispatch_sequence == query_events[0].sequence
        assert facts[0].owner == "btw"
        assert facts[0].usage is not None
        assert (facts[0].usage.input_tokens, facts[0].usage.output_tokens) == (40, 10)
        # The session bill is exactly one main turn plus one side query, never doubled.
        assert (report.input_tokens, report.output_tokens) == (140, 30)
        assert report.turn_count == 2

        # Closing and reopening the window bills the side query once: remaining 50, not 0.
        assert _command(core, session, "goal.pause", {}).status == "ok"
        resumed = _command(core, session, "goal.resume", {})
        assert resumed.status == "ok", resumed.error
        resumed_state = _goal_state(core, session)

    assert resumed_state["tokens_used"] == 50
    assert resumed_state["window_tokens_used"] == 0
    assert resumed_state["window_allowance"] == 50


def test_goal_marks_budget_limited_from_model_query_spend(tmp_path: Path) -> None:
    replies = [
        _usage_reply("goal setup turn", usage={"prompt_tokens": 10, "completion_tokens": 2}),
        _usage_reply("side answer", usage={"prompt_tokens": 30, "completion_tokens": 5}),
    ]
    with loopback_server(replies) as server:
        core = _open(tmp_path, server)
        core.auto_drain = False
        session = "sess-structured"
        assert core.run("start a budgeted goal session", session_id=session).status == "completed"
        assert _command(core, session, "goal.set", {"objective": "Stay within budget", "token_budget": 20}).status == "ok"
        assert core.governed_model_query(session, "spend the budget", owner="btw") == "side answer"
        state = _goal_state(core, session)
    assert state["status"] == "budget-limited"
    assert state["tokens_used"] == 35
    assert state["window_tokens_used"] == 35
    assert state["has_unknown_usage"] is False


def test_goal_keeps_unknown_model_query_usage_unknown(tmp_path: Path) -> None:
    replies = [
        _usage_reply("goal setup turn", usage={"prompt_tokens": 10, "completion_tokens": 2}),
        _usage_reply("side answer", usage=None),
    ]
    with loopback_server(replies) as server:
        core = _open(tmp_path, server)
        core.auto_drain = False
        session = "sess-structured"
        assert core.run("start a goal session", session_id=session).status == "completed"
        assert _command(core, session, "goal.set", {"objective": "Keep unknowns honest", "token_budget": 100}).status == "ok"
        assert core.governed_model_query(session, "unknown side usage", owner="btw") == "side answer"
        state = _goal_state(core, session)
        report = core.usage(session)
        facts = [fact for fact in _all_facts(core, session) if isinstance(fact, UsageFact) and fact.source == "model_query"]
    assert state["status"] == "usage-limited"
    assert state["tokens_used"] is None
    assert state["window_tokens_used"] is None
    assert state["has_unknown_usage"] is True
    assert len(facts) == 1 and facts[0].usage is None
    assert report.input_tokens is None and report.output_tokens is None
    assert report.has_unknown_usage is True
    assert report.turn_count == 2


def test_query_with_an_unexpected_tool_call_still_charges_its_usage(tmp_path: Path) -> None:
    reply = openai_response(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "unexpected", "type": "function", "function": {"name": "nope", "arguments": "{}"}}],
        },
        finish="tool_calls",
        usage={"prompt_tokens": 7, "completion_tokens": 2},
    )
    with loopback_server([reply]) as server:
        core = _open(tmp_path, server)
        with pytest.raises(ModelQueryToolCalls, match="returned tool calls"):
            core.governed_model_query("sess-structured", "bare", owner="find")
        report = core.usage("sess-structured")
    assert (report.input_tokens, report.output_tokens) == (7, 2)
    assert report.turn_count == 1
