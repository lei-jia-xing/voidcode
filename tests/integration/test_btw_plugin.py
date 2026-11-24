"""End-to-end integration tests for the btw plugin on the real stack."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

import pytest

from tests.fixtures.core_factory import loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core, CoreResult
from voidcode.core.storage.store import SequenceConflict, SessionStatus, StagedBudgetUpdate
from voidcode.plugins.btw.btw_plugin import (
    ANSWER_TRUNCATION_MARKER,
    MAX_ANSWER_BYTES,
    MAX_QUESTION_BYTES,
)
from voidcode.sdk.commands import CommandResult
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ProviderExecutionError, ProviderTurnRequest
from voidcode.sdk.services import PluginBudget

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins"
_NOTES_PLUGIN_SOURCE = '''"""Test-only plugin exposing one observable tool."""

from __future__ import annotations

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class NotesWriteTool:
    definition = ToolDefinition(
        name="notes_write",
        description="Write a note (test tool).",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text="notes written"))


def create_plugin(config):
    return Plugin(tools=(NotesWriteTool(),))
'''
_USAGE_OBSERVER_SOURCE = '''"""Test-only usage observer recording model-query facts."""

from __future__ import annotations

from voidcode.sdk.hooks import UsageFact
from voidcode.sdk.plugin import Plugin


def observe(fact, context, services):
    if isinstance(fact, UsageFact) and fact.source == "model_query":
        services.stage_event("side_usage", {"owner": fact.owner, "dispatch_sequence": fact.dispatch_sequence})


def create_plugin(config):
    return Plugin(observer=observe, observed_facts=("usage",))
'''


def _plugin_with_source(workspace: Path, name: str, source: str) -> Path:
    root = workspace / ".test-plugins" / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(source, encoding="utf-8")
    return root


def _notes_plugin(workspace: Path) -> Path:
    return _plugin_with_source(workspace, "notes", _NOTES_PLUGIN_SOURCE)


def _usage_observer_plugin(workspace: Path) -> Path:
    return _plugin_with_source(workspace, "usage_observer", _USAGE_OBSERVER_SOURCE)


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *plugin_ids: str,
    approval_mode: str = "yolo",
    read_only: bool = False,
    extra_plugins: Mapping[str, Path] | None = None,
) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {
        "files": {"enabled": False},
        "notes": {"path": str(_notes_plugin(workspace))},
    }
    for plugin_id in plugin_ids:
        plugins[plugin_id] = {"path": str(_PLUGIN_ROOT / plugin_id)}
    for plugin_id, path in (extra_plugins or {}).items():
        plugins[plugin_id] = {"path": str(path)}
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": approval_mode, "read_only": read_only},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": plugins,
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request.get("messages")
    return [entry for entry in messages if isinstance(entry, dict)] if isinstance(messages, list) else []


def _event_types(core: Core, session_id: str) -> list[str]:
    return [event.event_type for event in core.events_after(session_id)]


def _completion_payload(core: Core, session_id: str) -> dict[str, object]:
    event = next(event for event in core.events_after(session_id) if event.event_type == "core.command_completed")
    return json.loads(json.dumps(json_wire_object(event.payload)))


def test_btw_answers_without_polluting_history(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "first answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        ),
        openai_response(
            message={"role": "assistant", "content": "side answer"},
            finish="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        ),
        openai_response(message={"role": "assistant", "content": "follow-up answer"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"

        conversation_before = [event for event in core.events_after(session_id) if event.event_type in {"core.user_message", "core.model_turn"}]
        result = _invoke(core, session_id, "btw", {"question": "what is 2+2?"})
        assert result.status == "ok"
        assert result.output == "side answer"
        assert result.data == {"question": "what is 2+2?", "truncated": False}

        # The side question reads the conversation but offers no tools.
        assert len(server.requests) == 2
        btw_messages = _messages(server.requests[1])
        assert [message["role"] for message in btw_messages] == ["user", "assistant", "user"]
        prompt = str(btw_messages[-1]["content"])
        assert "what is 2+2?" in prompt
        assert btw_messages[0]["content"] == "main question"
        assert btw_messages[1]["content"] == "first answer"
        assert server.requests[1].get("tools") in (None, [])

        # The core records the canonical diagnostic fact; the plugin stages nothing.
        event_types = _event_types(core, session_id)
        assert "core.model_query" in event_types
        assert [
            event for event in core.events_after(session_id) if event.event_type in {"core.user_message", "core.model_turn"}
        ] == conversation_before
        assert not [event for event in event_types if event.startswith("plugin.btw.")]

        # The side query's real provider bill is part of the session report exactly once.
        query = next(event for event in core.events_after(session_id) if event.event_type == "core.model_query")
        assert query.payload["owner"] == "btw"
        report = core.usage(session_id)
        assert (report.input_tokens, report.output_tokens) == (22, 7)
        assert report.turn_count == 2
        assert report.has_unknown_usage is False

        # A later turn proves the side question and answer never enter provider history.
        assert core.run("follow up", session_id=session_id).status == "completed"
        follow_up_messages = _messages(server.requests[2])
        assert [message["role"] for message in follow_up_messages] == ["user", "assistant", "user"]
        rendered = json.dumps(follow_up_messages, ensure_ascii=False)
        assert "what is 2+2?" not in rendered
        assert "side answer" not in rendered
        assert follow_up_messages[-1]["content"] == "follow up"

        empty = _invoke(core, session_id, "btw", {"question": "   "})
        assert empty.status == "error"
        assert empty.error == "side question must not be empty"


def test_btw_query_cannot_bypass_an_exhausted_owner_budget(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        core.store.commit_staged(
            session_id,
            run_id="run-budget",
            revision_id=core.revision_id,
            events=(),
            status=SessionStatus.IDLE,
            staged_budgets=(
                StagedBudgetUpdate(
                    owner="btw",
                    action="set",
                    budget=PluginBudget(owner="btw", max_total_tokens=0),
                ),
            ),
        )
        result = _invoke(core, session_id, "btw", {"question": "budgeted?"})
        report = core.usage(session_id)
    assert result.status == "error"
    assert result.error == "side question blocked by an active budget"
    assert result.data == {"error": "budget"}
    assert server.requests == []
    assert (report.input_tokens, report.output_tokens) == (0, 0)
    assert report.turn_count == 0


def test_btw_uses_complete_context_while_tool_approval_is_pending(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "pending", "type": "function", "function": {"name": "notes_write", "arguments": "{}"}}],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "side answer"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "main finished"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw", approval_mode="ask")
        session_id = core.create_session().session_id
        main = core.run("write a note", session_id=session_id)
        assert main.pending_approval is not None
        before = core.session(session_id)
        result = _invoke(core, session_id, "btw", {"question": "what did I request?"})
        assert result.status == "ok"
        messages = _messages(server.requests[1])
        assert [message["role"] for message in messages] == ["user", "user"]
        assert messages[0]["content"] == "write a note"
        assert not any(message.get("tool_calls") for message in messages)
        after = core.session(session_id)
        assert after.status == before.status
        assert after.run_id == before.run_id

        # The parked run must resume and complete after the side-query events landed.
        request_id = str(main.pending_approval["request_id"])
        resumed = core.resume(session_id, approval_request_id=request_id, decision="allow")
        assert resumed.status == "completed"
        assert resumed.output == "main finished"
        resumed_messages = _messages(server.requests[2])
        assert [message["role"] for message in resumed_messages] == ["user", "assistant", "tool"]
        rendered = json.dumps(resumed_messages, ensure_ascii=False)
        assert "what did I request?" not in rendered
        assert "side answer" not in rendered


def test_btw_runs_while_the_main_turn_is_running(tmp_path: Path) -> None:
    main_started = threading.Event()
    release_main = threading.Event()
    main_reply = openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop")
    side_reply = openai_response(message={"role": "assistant", "content": "side answer"}, finish="stop")

    def before_reply(request: dict[str, object], _number: int) -> dict[str, object]:
        if request.get("method") == "GET":
            return {"object": "list", "data": []}
        messages = _messages(request)
        if any("side question from the user" in str(message.get("content") or "") for message in messages):
            return side_reply
        main_started.set()
        assert release_main.wait(timeout=10)
        return main_reply

    with loopback_server([]) as server:
        server.before_reply = before_reply
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        main_results: list[CoreResult] = []
        worker = threading.Thread(target=lambda: main_results.append(core.run("main question", session_id=session_id)))
        worker.start()
        try:
            assert main_started.wait(timeout=10)
            assert core.session(session_id).status is SessionStatus.RUNNING
            result = _invoke(core, session_id, "btw", {"question": "what now?"})
            assert result.status == "ok"
            assert result.output == "side answer"
            assert "core.model_query" in _event_types(core, session_id)
            assert "core.command_completed" in _event_types(core, session_id)
            assert core.session(session_id).status is SessionStatus.RUNNING
        finally:
            release_main.set()
            worker.join(timeout=10)
        assert not worker.is_alive()
        assert len(main_results) == 1
        assert main_results[0].status == "completed"
        assert core.session(session_id).status is SessionStatus.COMPLETED


def test_btw_retries_side_commits_after_sequence_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "side answer"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"

        original = core.store.commit_staged
        injected = {"model_query": False, "command_completed": False}

        def flaky_commit(session_id: str, **kwargs: Any) -> object:
            events = kwargs.get("events", ())
            for event in events:
                event_type = getattr(event, "event_type", "")
                if event_type == "core.model_query" and not injected["model_query"]:
                    injected["model_query"] = True
                    raise SequenceConflict("simulated concurrent tip move")
                if event_type == "core.command_completed" and not injected["command_completed"]:
                    injected["command_completed"] = True
                    raise SequenceConflict("simulated concurrent tip move")
            return original(session_id, **kwargs)

        monkeypatch.setattr(core.store, "commit_staged", flaky_commit)
        result = _invoke(core, session_id, "btw", {"question": "still safe?"})
        events = core.events_after(session_id)
        report = core.usage(session_id)

    assert result.status == "ok"
    assert result.output == "side answer"
    assert injected == {"model_query": True, "command_completed": True}
    assert len([event for event in events if event.event_type == "core.model_query"]) == 1
    assert len([event for event in events if event.event_type == "core.command_completed"]) == 1
    assert report.turn_count == 2


def test_btw_is_available_in_a_read_only_session(tmp_path: Path) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "side answer"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw", read_only=True)
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"
        assert core.session_read_only(session_id) is True
        result = _invoke(core, session_id, "btw", {"question": "allowed?"})
    assert result.status == "ok"
    assert result.output == "side answer"


def test_btw_cancellation_stops_the_side_provider_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"

        provider, _owner = core.plugin_runtime._providers["endpoint"]  # type: ignore[attr-defined]
        started = threading.Event()

        def blocked_propose_turn(request: ProviderTurnRequest) -> NoReturn:
            abort = request.abort_signal
            assert abort is not None
            started.set()
            deadline = time.monotonic() + 10
            while not abort.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            assert abort.cancelled, "cancellation never reached the provider request"
            raise ProviderExecutionError(
                kind="cancelled",
                provider_name=request.provider_name or "endpoint",
                model_name=request.model_name or "test-model",
                message="cancelled",
                retryable=False,
            )

        monkeypatch.setattr(provider, "propose_turn", blocked_propose_turn)
        results: list[CommandResult] = []
        worker = threading.Thread(target=lambda: results.append(_invoke(core, session_id, "btw", {"question": "cancel me"})))
        worker.start()
        assert started.wait(timeout=10)
        assert core.cancel_session(session_id) is True
        worker.join(timeout=10)
        events = core.events_after(session_id)
        payload = _completion_payload(core, session_id)

    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].status == "error"
    assert "cancelled" in (results[0].error or "")
    assert payload["cancelled"] is True
    assert payload["result"]["output"] is None
    assert payload["result"]["data"] == {"cancelled": True}
    assert not [event for event in events if event.event_type == "core.model_query"]


def test_btw_tool_call_reply_is_a_typed_error_and_never_an_answer(tmp_path: Path) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop"),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "side-call", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
            },
            finish="tool_calls",
        ),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"
        result = _invoke(core, session_id, "btw", {"question": "call a tool?"})
        payload = _completion_payload(core, session_id)
        report = core.usage(session_id)
    assert result.status == "error"
    assert result.error == "side question was answered with a tool call instead of text"
    assert result.data == {"error": "tool_call"}
    assert payload["result"]["output"] is None
    assert report.turn_count == 2


def test_btw_oversize_question_is_refused_before_the_provider(tmp_path: Path) -> None:
    replies = [openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop")]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"
        question = "😀" * (MAX_QUESTION_BYTES // 4 + 1)
        result = _invoke(core, session_id, "btw", {"question": question})
        requests = len(server.requests)
    assert result.status == "error"
    assert result.error == "side question is too long for the 64 KiB prompt limit"
    assert result.data == {"error": "question_too_long"}
    assert requests == 1


def test_btw_oversize_answer_is_clipped_and_stored_once(tmp_path: Path) -> None:
    long_answer = "y" * (MAX_ANSWER_BYTES + 1000)
    replies = [
        openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": long_answer}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"
        result = _invoke(core, session_id, "btw", {"question": "give me everything"})
        payload = _completion_payload(core, session_id)
    assert result.status == "ok"
    assert result.output is not None and result.output.endswith(ANSWER_TRUNCATION_MARKER)
    assert len(result.output.encode("utf-8")) <= MAX_ANSWER_BYTES
    assert result.data == {"question": "give me everything", "truncated": True}
    assert "answer" not in result.data
    assert payload["result"]["output"] == result.output
    assert "answer" not in payload["result"]["data"]


def test_btw_usage_fact_reaches_usage_observers_once(tmp_path: Path) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "side answer"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        workspace = tmp_path / "workspace"
        observer = _usage_observer_plugin(workspace)
        core = _core(tmp_path, server, "btw", extra_plugins={"usage_observer": observer})
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"
        assert _invoke(core, session_id, "btw", {"question": "observe me"}).status == "ok"
        events = core.events_after(session_id)
    query = next(event for event in events if event.event_type == "core.model_query")
    observed = [event for event in events if event.event_type == "plugin.usage_observer.side_usage"]
    assert len(observed) == 1
    assert observed[0].payload == {"owner": "btw", "dispatch_sequence": query.sequence}


def test_btw_does_not_reserve_root_action_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        openai_response(message={"role": "assistant", "content": "main answer"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "side answer"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"

        original = core.store.commit_staged

        def guarded_commit(session_id: str, **kwargs: Any) -> object:
            assert kwargs.get("root_reservation") is None, "btw reserved a root action unit"
            return original(session_id, **kwargs)

        monkeypatch.setattr(core.store, "commit_staged", guarded_commit)
        result = _invoke(core, session_id, "btw", {"question": "not a root action"})
    assert result.status == "ok"
    assert result.output == "side answer"
