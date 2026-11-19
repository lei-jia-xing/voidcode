"""Real-Core regressions for model-initiated persistent ask (Contract B)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.events import EventType
from voidcode.sdk.ask import AskError, AskRequest, AskValidationError
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import TextOutput, ToolCall, ToolFailure, ToolSuccess

PLUGIN_ID = "ask-fixture"

_PLUGIN_CODE = """
from __future__ import annotations

from voidcode.sdk.ask import AskMultiSelect, AskRequestDraft, SuspendAsk
from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.foreground import CompositionDraft
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess
from voidcode.sdk.views import ChoiceOption, FormField


def _question(raw):
    kind = raw.get("kind", "field")
    if kind == "multi":
        return AskMultiSelect(
            name=raw["name"],
            label=raw.get("label", raw["name"]),
            options=tuple(ChoiceOption(id=item["id"], label=item.get("label", item["id"])) for item in raw["options"]),
            required=bool(raw.get("required", True)),
            min_selections=int(raw.get("min_selections", 0)),
            max_selections=raw.get("max_selections"),
        )
    if kind != "field":
        raise ValueError(f"unknown question kind: {kind!r}")
    return FormField(
        name=raw["name"],
        label=raw.get("label", raw["name"]),
        field_type=raw.get("field_type", "text"),
        required=bool(raw.get("required", True)),
    )


def _draft(arguments):
    return AskRequestDraft(
        title=str(arguments.get("title", "Untitled ask")),
        questions=tuple(_question(raw) for raw in arguments.get("questions", ())),
        timeout_ms=arguments.get("timeout_ms"),
    )


class AskProbe:
    @property
    def definition(self):
        return ToolDefinition(name="ask_probe", description="ask fixture", effects=frozenset({ToolEffect.SESSION}))

    def invoke(self, call, *, context, services):
        services.suspend_for_ask(_draft(call.arguments))
        raise AssertionError("suspend_for_ask returned")


class AskTwice:
    @property
    def definition(self):
        return ToolDefinition(name="ask_twice", description="double suspend", effects=frozenset({ToolEffect.SESSION}))

    def invoke(self, call, *, context, services):
        try:
            services.suspend_for_ask(_draft(call.arguments))
        except SuspendAsk:
            services.suspend_for_ask(_draft(call.arguments))
        raise AssertionError("second suspend_for_ask returned")


class SwallowAsk:
    @property
    def definition(self):
        return ToolDefinition(name="swallow_ask", description="swallowed suspend", effects=frozenset({ToolEffect.SESSION}))

    def invoke(self, call, *, context, services):
        try:
            services.suspend_for_ask(_draft(call.arguments))
        except BaseException:
            return ToolSuccess("swallow_ask", TextOutput("swallowed"))
        raise AssertionError("suspend_for_ask returned")


class ComposeProbe:
    @property
    def definition(self):
        return ToolDefinition(name="compose_probe", description="composition stub", effects=frozenset({ToolEffect.SESSION}))

    def invoke(self, call, *, context, services):
        services.suspend_foreground_composition(CompositionDraft(cell_source="1", namespace_key="ns"))
        raise AssertionError("suspend_foreground_composition returned")


class WriteProbe:
    @property
    def definition(self):
        return ToolDefinition(name="write_probe", description="approval probe", effects=frozenset({ToolEffect.WRITE}))

    def invoke(self, call, *, context, services):
        return ToolSuccess("write_probe", TextOutput("wrote"))


class AskSuspendCommand:
    @property
    def definition(self):
        return CommandDefinition(name="ask_suspend_cmd", description="suspend from command")

    def invoke(self, invocation, services):
        draft = AskRequestDraft(title="cmd", questions=(FormField(name="q", label="Q"),))
        services.suspend_for_ask(draft)
        return CommandResult(status="ok", output="unreachable")


class AskEnqueueBg:
    @property
    def definition(self):
        return CommandDefinition(name="ask_enqueue_bg", description="enqueue bg suspender")

    def invoke(self, invocation, services):
        action_id = services.enqueue_background_action({"note": "ask-bg"})
        return CommandResult(status="ok", data={"action_id": action_id})


def _bg_driver(context):
    draft = AskRequestDraft(title="bg", questions=(FormField(name="q", label="Q"),))
    try:
        context.invoke(lambda ctx, services: services.suspend_for_ask(draft))
    except Exception as exc:
        def save(_ctx, services):
            value, version = services.get_state("session")
            state = dict(value) if value else {}
            state["bg_refusal"] = f"{type(exc).__name__}: {exc}"
            services.replace_state(state, expected_version=version)

        context.invoke(save)
    else:
        raise AssertionError("background suspend_for_ask returned")


def create_plugin(config):
    return Plugin(
        tools=(AskProbe(), AskTwice(), SwallowAsk(), ComposeProbe(), WriteProbe()),
        commands=(AskSuspendCommand(), AskEnqueueBg()),
        background_action=_bg_driver,
    )
"""

_HOOK_PLUGIN_CODE = """
from __future__ import annotations

from voidcode.sdk.ask import AskRequestDraft
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.views import FormField


def _hook(lifecycle, context, services):
    services.suspend_for_ask(AskRequestDraft(title="hook", questions=(FormField(name="q", label="Q"),)))
    return None


def create_plugin(config):
    return Plugin(session_start=_hook)
"""


def _call(identity: str, tool_name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": list(calls)}, finish="tool_calls" if calls else "stop")


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _configure(tmp_path: Path, server: OpenAILoopbackServer, *, approval: str = "yolo", plugin_code: str = _PLUGIN_CODE) -> Path:
    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(plugin_code, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": approval, "read_only": False},
                "providers": {"endpoint": {"api_key": "ask-fixture-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source)}},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _open(tmp_path: Path) -> Core:
    return open_core(tmp_path / "workspace", state_dir=tmp_path / "state")


def _wait_for(predicate: Callable[[], bool], *, what: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _questions(tag: str) -> list[dict[str, object]]:
    return [
        {"kind": "field", "name": "q1", "label": f"First {tag}", "field_type": "text", "required": True},
        {"kind": "field", "name": "n", "label": f"Count {tag}", "field_type": "integer", "required": True},
        {
            "kind": "multi",
            "name": "tags",
            "label": f"Tags {tag}",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
            "required": True,
            "min_selections": 1,
        },
    ]


def _completions(core: Core, session_id: str) -> list[Any]:
    return [event for event in core.store.events(session_id) if event.event_type == EventType.TOOL_COMPLETED and not event.payload.get("deferred")]


def test_ask_park_answer_adopts_typed_body_and_continues(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one")), output=None), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            assert parked.status == "waiting", parked
            assert parked.pending_ask is not None
            request_id = str(parked.pending_ask["request"]["request_id"])
            assert parked.as_payload()["pending_ask"]["request"]["request_id"] == request_id
            requested = [event for event in core.store.events("s") if event.event_type == EventType.ASK_REQUESTED]
            assert len(requested) == 1
            checkpoint = core.store.checkpoint("s")
            assert checkpoint is not None and checkpoint.kind == "ready"
            assert checkpoint.payload["ask_sequence"] == requested[0].sequence
            assert checkpoint.payload["ask_request_id"] == request_id
            answered = core.answer_ask("s", request_id, {"q1": "hello", "n": 3, "tags": ["a", "b"]}, expected_revision=core.revision_id)
            assert answered.status == "completed", answered
            done = _completions(core, "s")
            assert len(done) == 1
            result = decode_tool_result(done[0].payload["result"])
            assert isinstance(result, ToolSuccess)
            assert result.body is not None
            assert result.body.as_payload() == {"answers": {"q1": "hello", "n": 3, "tags": ["a", "b"]}, "status": "answered"}
            assert isinstance(result.output, TextOutput)
            assert "First one: hello" in result.output.text and "Count one: 3" in result.output.text and "Tags one:" in result.output.text
            intents = [
                event
                for event in core.store.events("s")
                if event.event_type == EventType.TOOL_INTENT and event.payload["call"]["tool_name"] == "ask_probe"
            ]
            assert len(intents) == 1
            cleared = core.store.checkpoint("s")
            assert cleared is not None
            assert cleared.payload["ask_sequence"] is None and cleared.payload["ask_request_id"] is None


def test_ask_cancel_adopts_failure_and_continues(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            assert parked.status == "waiting"
            request_id = str(parked.pending_ask["request"]["request_id"])
            cancelled = core.cancel_ask("s", request_id, expected_revision=core.revision_id)
            assert cancelled.status == "completed", cancelled
            done = _completions(core, "s")
            assert len(done) == 1
            result = decode_tool_result(done[0].payload["result"])
            assert isinstance(result, ToolFailure) and "cancelled by the user" in result.error
            answered = [event for event in core.store.events("s") if event.event_type == EventType.ASK_ANSWERED]
            assert len(answered) == 1 and answered[0].payload["status"] == "cancelled"


def test_ask_double_answer_refused(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            request_id = str(parked.pending_ask["request"]["request_id"])
            core.answer_ask("s", request_id, {"q1": "hello", "n": 3, "tags": ["a"]}, expected_revision=core.revision_id)
            with pytest.raises(AskError, match="already answered"):
                core.answer_ask("s", request_id, {"q1": "again", "n": 4, "tags": ["b"]}, expected_revision=core.revision_id)
            with pytest.raises(AskError, match="already answered"):
                core.cancel_ask("s", request_id, expected_revision=core.revision_id)
            assert len([event for event in core.store.events("s") if event.event_type == EventType.ASK_ANSWERED]) == 1


def test_ask_unknown_request_refused(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            request_id = str(parked.pending_ask["request"]["request_id"])
            with pytest.raises(AskError, match="not the live parked ask"):
                core.answer_ask("s", "nope", {"q1": "x", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            with pytest.raises(ValueError, match="stale"):
                core.answer_ask("s", request_id, {"q1": "x", "n": 1, "tags": ["a"]}, expected_revision="deadbeef")
            done = core.answer_ask("s", request_id, {"q1": "x", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            assert done.status == "completed"


def test_ask_validation_failure_keeps_park(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            request_id = str(parked.pending_ask["request"]["request_id"])
            with pytest.raises(AskValidationError):
                core.answer_ask("s", request_id, {"q1": "", "n": "abc", "tags": []}, expected_revision=core.revision_id)
            assert core.session("s").status == "waiting"
            assert len([event for event in core.store.events("s") if event.event_type == EventType.ASK_ANSWERED]) == 0
            done = core.answer_ask("s", request_id, {"q1": "ok", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            assert done.status == "completed"


def test_ask_expiry_sweep_settles_typed_failure(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"), timeout_ms=50)), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            assert parked.status == "waiting"
            request_id = str(parked.pending_ask["request"]["request_id"])
            time.sleep(0.15)
            core._sweep_expired_asks()
            answered = [event for event in core.store.events("s") if event.event_type == EventType.ASK_ANSWERED]
            assert len(answered) == 1 and answered[0].payload["status"] == "expired"
            assert answered[0].payload["request_id"] == request_id
            done = _completions(core, "s")
            assert len(done) == 1
            result = decode_tool_result(done[0].payload["result"])
            assert isinstance(result, ToolFailure) and "expired without an answer" in result.error
            assert core.session("s").status == "completed"
            with pytest.raises(AskError, match="expir"):
                core.answer_ask("s", request_id, {"q1": "late", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)


def test_ask_late_answer_enforces_expiry(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"), timeout_ms=50)), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            request_id = str(parked.pending_ask["request"]["request_id"])
            time.sleep(0.15)
            with pytest.raises(AskError, match="expir"):
                core.answer_ask("s", request_id, {"q1": "late", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            answered = [event for event in core.store.events("s") if event.event_type == EventType.ASK_ANSWERED]
            assert len(answered) == 1 and answered[0].payload["status"] == "expired"
            assert core.session("s").status == "completed"


def test_ask_unattended_refused_no_park(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            result = core.run("ask me", session_id="s", interaction="unattended")
            assert result.status == "completed", result
            assert result.pending_ask is None and "pending_ask" not in result.as_payload()
            assert len([event for event in core.store.events("s") if event.event_type == EventType.ASK_REQUESTED]) == 0
            done = _completions(core, "s")
            assert len(done) == 1
            outcome = decode_tool_result(done[0].payload["result"])
            assert isinstance(outcome, ToolFailure) and "unsupported while unattended" in outcome.error


def test_approval_unattended_refused_run_continues(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "write_probe")), _turn(output="done")]) as server:
        _configure(tmp_path, server, approval="ask")
        with _open(tmp_path) as core:
            core.create_session("s")
            result = core.run("write it", session_id="s", interaction="unattended")
            assert result.status == "completed", result
            assert len([event for event in core.store.events("s") if event.event_type == EventType.APPROVAL_REQUESTED]) == 0
            done = _completions(core, "s")
            assert len(done) == 1
            outcome = decode_tool_result(done[0].payload["result"])
            assert isinstance(outcome, ToolFailure) and "approval is unsupported while unattended" in outcome.error


def test_resume_interaction_overrides_mode(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "write_probe"), _call("c2", "write_probe")), _turn(output="done")]) as server:
        _configure(tmp_path, server, approval="ask")
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("write twice", session_id="s")
            assert parked.status == "waiting"
            approvals = [event for event in core.store.events("s") if event.event_type == EventType.APPROVAL_REQUESTED]
            assert len(approvals) == 1
            resumed = core.resume(
                "s",
                approval_request_id=str(approvals[0].payload["request_id"]),
                decision="allow",
                interaction="unattended",
            )
            assert resumed.status == "completed", resumed
            approvals = [event for event in core.store.events("s") if event.event_type == EventType.APPROVAL_REQUESTED]
            assert len(approvals) == 1
            done = _completions(core, "s")
            assert len(done) == 2
            first = decode_tool_result(done[0].payload["result"])
            second = decode_tool_result(done[1].payload["result"])
            assert isinstance(first, ToolSuccess)
            assert isinstance(second, ToolFailure) and "approval is unsupported while unattended" in second.error


def test_ask_sequential_parks_resolve_in_order(tmp_path: Path) -> None:
    with _server(
        [
            _turn(
                _call("c1", "ask_probe", title="First", questions=_questions("one")),
                _call("c2", "ask_probe", title="Second", questions=_questions("two")),
            ),
            _turn(output="done"),
        ]
    ) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask twice", session_id="s")
            assert parked.status == "waiting"
            first_id = str(parked.pending_ask["request"]["request_id"])
            assert str(parked.pending_ask["request"]["title"]) == "First"
            again = core.answer_ask("s", first_id, {"q1": "one", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            assert again.status == "waiting"
            assert again.pending_ask is not None
            second_id = str(again.pending_ask["request"]["request_id"])
            assert second_id != first_id
            assert str(again.pending_ask["request"]["title"]) == "Second"
            done = core.answer_ask("s", second_id, {"q1": "two", "n": 2, "tags": ["b"]}, expected_revision=core.revision_id)
            assert done.status == "completed", done
            assert len(_completions(core, "s")) == 2


def test_ask_second_suspend_while_parked_refused(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        workspace = _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            assert parked.status == "waiting"
            context = ToolContext(
                workspace=workspace,
                session_id="s",
                run_id=uuid4().hex,
                invocation_id=uuid4().hex,
                revision_id=core.revision_id,
                actor="model",
            )
            outcome = core.plugin_runtime.invoke(
                ToolCall("ask_probe", {"title": "Second", "questions": _questions("two")}),
                context=context,
                readonly=False,
            )
            assert isinstance(outcome, ToolFailure) and "AskAlreadyParked" in outcome.error
            assert len([event for event in core.store.events("s") if event.event_type == EventType.ASK_REQUESTED]) == 1
            request_id = str(parked.pending_ask["request"]["request_id"])
            done = core.answer_ask("s", request_id, {"q1": "x", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            assert done.status == "completed"


def test_ask_second_stage_on_lease_refused(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_twice", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            result = core.run("ask twice", session_id="s")
            assert result.status == "completed", result
            assert len([event for event in core.store.events("s") if event.event_type == EventType.ASK_REQUESTED]) == 0
            done = _completions(core, "s")
            assert len(done) == 1
            outcome = decode_tool_result(done[0].payload["result"])
            assert isinstance(outcome, ToolFailure) and "AskAlreadyParked" in outcome.error


def test_ask_swallowed_suspend_fails_visibly(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "swallow_ask", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            result = core.run("swallow", session_id="s")
            assert result.status == "completed", result
            assert len([event for event in core.store.events("s") if event.event_type == EventType.ASK_REQUESTED]) == 0
            done = _completions(core, "s")
            assert len(done) == 1
            outcome = decode_tool_result(done[0].payload["result"])
            assert isinstance(outcome, ToolFailure) and "swallowed" in outcome.error


def test_composition_suspend_stub_visible(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "compose_probe")), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            result = core.run("compose", session_id="s")
            assert result.status == "completed", result
            done = _completions(core, "s")
            assert len(done) == 1
            outcome = decode_tool_result(done[0].payload["result"])
            assert isinstance(outcome, ToolFailure) and "not implemented" in outcome.error


def test_ask_suspend_outside_tool_refused(tmp_path: Path) -> None:
    with _server([]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            refused = core.invoke_command("s", "ask_suspend_cmd", {}, expected_revision=core.revision_id)
            assert refused.status == "error" and refused.error is not None and "tool-callback lease" in refused.error
            queued = core.invoke_command("s", "ask_enqueue_bg", {}, expected_revision=core.revision_id)
            assert queued.status == "ok", queued
            _wait_for(
                lambda: (
                    core.store.plugin_state(PLUGIN_ID, "session", "s")[0] is not None
                    and "bg_refusal" in (core.store.plugin_state(PLUGIN_ID, "session", "s")[0] or {})
                ),
                what="background suspend refusal",
            )
            state, _version = core.store.plugin_state(PLUGIN_ID, "session", "s")
            assert state is not None and "tool-callback lease" in str(state["bg_refusal"])


def test_ask_suspend_from_hook_refused(tmp_path: Path) -> None:
    with _server([]) as server:
        _configure(tmp_path, server, plugin_code=_HOOK_PLUGIN_CODE)
        with _open(tmp_path) as core:
            with pytest.raises(RuntimeError, match="tool-callback lease"):
                core.plugin_runtime.run_lifecycle(PLUGIN_ID, "session_start", session_id="s", run_id="r")


def test_ask_session_cancel_while_parked_settles_interrupted(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            assert parked.status == "waiting"
            request_id = str(parked.pending_ask["request"]["request_id"])
            assert core.cancel_session("s") is True
            assert core.session("s").status == "interrupted"
            interrupted = [event for event in core.store.events("s") if event.event_type == EventType.RUN_INTERRUPTED]
            assert len(interrupted) == 1 and interrupted[0].payload["stop_cause"] == "user_stop"
            assert any(event.event_type == EventType.TOOL_INTERRUPTED for event in core.store.events("s"))
            with pytest.raises(AskError):
                core.answer_ask("s", request_id, {"q1": "x", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)


def test_ask_reconnect_rerenders_request(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            assert parked.status == "waiting"
            request_id = str(parked.pending_ask["request"]["request_id"])
        with _open(tmp_path) as core:
            requested = [event for event in core.store.events("s") if event.event_type == EventType.ASK_REQUESTED]
            assert len(requested) == 1
            request = AskRequest.from_payload(requested[0].payload["request"])
            assert request.request_id == request_id and request.title == "Pick"
            assert [question.name for question in request.questions] == ["q1", "n", "tags"]
            done = core.answer_ask("s", request_id, {"q1": "x", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            assert done.status == "completed", done


def test_resume_refuses_ask_parked_session(tmp_path: Path) -> None:
    with _server([_turn(_call("c1", "ask_probe", title="Pick", questions=_questions("one"))), _turn(output="done")]) as server:
        _configure(tmp_path, server)
        with _open(tmp_path) as core:
            core.create_session("s")
            parked = core.run("ask me", session_id="s")
            assert parked.status == "waiting"
            request_id = str(parked.pending_ask["request"]["request_id"])
            with pytest.raises(ValueError, match="ask.answer"):
                core.resume("s")
            assert core.session("s").status == "waiting"
            done = core.answer_ask("s", request_id, {"q1": "x", "n": 1, "tags": ["a"]}, expected_revision=core.revision_id)
            assert done.status == "completed", done
            intents = [
                event
                for event in core.store.events("s")
                if event.event_type == EventType.TOOL_INTENT and event.payload["call"]["tool_name"] == "ask_probe"
            ]
            assert len(intents) == 1
