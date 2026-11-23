"""End-to-end tests for the compaction plugin on the real core stack.

Drives the real core, plugin runtime, loopback provider and durable storage:

- a run whose dispatch reports enough prompt tokens makes the plugin declare a window
  projection; the core verifies the boundary and measures the covered/dropped bytes itself
- the next dispatch substitutes one ``[window summary by compaction; ...]`` system message for
  the covered prefix, and the covered prompt text is gone from the request
- history is never rewritten: every committed event is still readable, in order
- the manual ``/compact`` command projects on demand, reports the same accounting the core
  holds, and a second call with nothing new to cover is a no-op

The probe plugin below exists only to give the session real native tool batches (a write and a
read over one workspace file) for the projection to cover; the plugin under test is the real
bundled source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import jsonschema

from tests.fixtures.core_factory import discard_core, loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.json_values import json_wire_object

_COMPACTION_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "compaction"
_PROBE_PLUGIN_ID = "compaction-probe"
_WRITE_TOOL = "probe_write"
_READ_TOOL = "probe_read"
#: Enough text that covering it always shrinks the window, whatever the previews keep.
_NOTES_TEXT = "".join(f"line {index}: the parser keeps allocation-free tokens\n" for index in range(60))
_SECRET_MARKER = "SECOND-PROMPT-COVERED-SECRET"
#: The core's own substitution label, exactly as the assembly seam renders it.
_SUMMARY_LABEL = re.compile(r"\[window summary by compaction; (\d+) covered events \((\d+) bytes -> (\d+) dropped\)\]\n(.*)", re.DOTALL)

_PROBE_PLUGIN_SOURCE = '''"""Test-only probe plugin: one write tool and one read tool over workspace files."""

from __future__ import annotations

from typing import ClassVar

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
    "required": ["path", "text"],
    "additionalProperties": False,
}
_READ_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


class ProbeWriteTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="probe_write",
        description="Write text to a file in the workspace (test tool).",
        input_schema=_WRITE_SCHEMA,
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        _ = services
        path = str(call.arguments["path"])
        text = str(call.arguments["text"])
        target = context.workspace / path
        target.write_text(text, encoding="utf-8")
        return ToolSuccess(call.tool_name, TextOutput(f"wrote {len(text)} chars to {path}"))


class ProbeReadTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="probe_read",
        description="Read a file from the workspace (test tool).",
        input_schema=_READ_SCHEMA,
        effects=frozenset({ToolEffect.READ}),
        replay_policy="safe",
    )

    def invoke(self, call, *, context, services):
        _ = services
        path = str(call.arguments["path"])
        return ToolSuccess(call.tool_name, TextOutput((context.workspace / path).read_text(encoding="utf-8")))


def create_plugin(config=None):
    _ = config
    return Plugin(tools=(ProbeWriteTool(), ProbeReadTool()))
'''


def _probe_source(tmp_path: Path) -> Path:
    """The inline probe plugin: real tool batches for the projection to cover."""
    root = tmp_path / "probe-plugin"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(_PROBE_PLUGIN_SOURCE, encoding="utf-8")
    return root


_PROJECTION_OBSERVER_SOURCE = '''"""Test-only observer for context_projected facts."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin, StateSchema
from voidcode.sdk.services import PluginServices


def observer(fact, context, services: PluginServices) -> None:
    _ = context
    state, version = services.get_state("session")
    payload = dict(state or {})
    payload["observed_sequence"] = fact.event_sequence
    services.replace_state(payload, expected_version=version, scope="session")


def create_plugin(config: Mapping[str, object]) -> Plugin:
    _ = config
    return Plugin(observer=observer, observed_facts=("context_projected",), state=StateSchema(version=1))
'''


def _projection_observer_source(tmp_path: Path) -> Path:
    root = tmp_path / "projection-observer"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(_PROJECTION_OBSERVER_SOURCE, encoding="utf-8")
    return root


def _core(tmp_path: Path, server: OpenAILoopbackServer, *, threshold_tokens: int = 1_000, observer: bool = False) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        # The probe tool writes, so the session runs with auto-approval.
        "policy": {"approval_mode": "yolo"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            _PROBE_PLUGIN_ID: {"path": str(_probe_source(tmp_path))},
            "compaction": {
                "path": str(_COMPACTION_ROOT),
                "config": {
                    "enabled": True,
                    "threshold_tokens": threshold_tokens,
                    # A fixed gate: the trigger is exactly ``threshold_tokens``.
                    "reserve_tokens": 0,
                    # Retain nothing: the covered prefix is then everything coverable.
                    "keep_recent_tokens": 0,
                },
            },
        },
    }
    if observer:
        config["plugins"]["projection-observer"] = {"path": str(_projection_observer_source(tmp_path))}
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
    return core


def _tool_call(identity: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None, prompt_tokens: int = 5_000) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 0}},
    )


def _messages(request: Mapping[str, object]) -> list[Mapping[str, object]]:
    messages = request.get("messages")
    assert isinstance(messages, list)
    return [message for message in messages if isinstance(message, Mapping)]


def _summary_messages(request: Mapping[str, object]) -> list[str]:
    return [
        str(message.get("content", ""))
        for message in _messages(request)
        if message.get("role") == "system" and str(message.get("content", "")).startswith("[window summary by")
    ]


def _request_text(request: Mapping[str, object]) -> str:
    return json.dumps([dict(message) for message in _messages(request)], default=str)


def _command(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def test_a_crossed_threshold_projects_the_window_and_keeps_history(tmp_path: Path) -> None:
    def _summary(text: str) -> dict[str, object]:
        return openai_response(message={"role": "assistant", "content": text}, finish="stop")

    replies = [
        _turn(_tool_call("call-1", _WRITE_TOOL, {"path": "notes.txt", "text": _NOTES_TEXT})),
        _summary("the probe wrote its notes"),
        _turn(output="notes written"),
        _summary("the probe wrote and read its notes"),
        _turn(_tool_call("call-2", _READ_TOOL, {"path": "notes.txt"})),
        _summary("the probe wrote, read, and answered"),
        _turn(output="notes read"),
        _summary("the probe finished both batches"),
        _turn(output="third answer"),
        _summary("the probe kept going"),
        _turn(output="fourth answer"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session("sess-compaction").session_id
        first_prompt = "first prompt: start the probe"
        second_prompt = f"second prompt: read the notes back {_SECRET_MARKER}"

        assert core.run(first_prompt, session_id=session_id).status == "completed"
        first_sequences = [event.sequence for event in core.events_after(session_id)]

        # The first dispatch had no projection to substitute, so nothing was summarised yet.
        assert _summary_messages(server.requests[0]) == []
        # From the second dispatch on, the plugin's projection is in force (request 1 is the
        # tool-free summary turn the first projection consumed).
        assert len(_summary_messages(server.requests[2])) == 1

        assert core.run(second_prompt, session_id=session_id).status == "completed"

        # (a) The core holds the projection and its measured accounting.
        projection = core.store.window_projection(session_id)
        assert projection is not None
        assert projection.owner == "compaction"
        assert projection.covered_from_sequence == first_sequences[0]
        assert projection.covered_to_sequence >= first_sequences[-1]
        assert projection.covered_bytes > len(projection.replacement.encode("utf-8")) > 0
        assert projection.dropped_bytes == projection.covered_bytes - len(projection.replacement.encode("utf-8"))
        assert projection.replacement.startswith("Runtime context projection:")
        # The committed replacement is model-produced: it carries the latest summary turn's answer.
        assert "the probe finished both batches" in projection.replacement

        # (b) The dispatch that followed the projection carries the substituted summary, and
        # the covered prompt text is gone from it. The numbers are the core's own: the label
        # and the replacement agree on the bytes the projection measured.
        # The last request is the final projection's own summary turn; the substituted dispatch
        # is the newest request that actually carries the summary message.
        sent = next(request for request in reversed(server.requests) if _summary_messages(request))
        summaries = _summary_messages(sent)
        assert len(summaries) == 1
        labelled = _SUMMARY_LABEL.fullmatch(summaries[0])
        assert labelled is not None, summaries[0][:120]
        covered_events = int(labelled.group(1))
        covered_bytes = int(labelled.group(2))
        dropped_bytes = int(labelled.group(3))
        replacement = labelled.group(4)
        # The whole first run (its prompt included) is inside the covered prefix.
        assert covered_events >= first_sequences[-1]
        assert replacement.startswith("Runtime context projection:")
        assert dropped_bytes == covered_bytes - len(replacement.encode("utf-8"))
        assert _SECRET_MARKER not in _request_text(sent)
        assert _NOTES_TEXT not in _request_text(sent)

        # (c) History is complete and ordered: the projection rewrote nothing.
        events = core.events_after(session_id)
        sequences = [event.sequence for event in events]
        assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))
        assert sequences[: len(first_sequences)] == first_sequences
        assert len(sequences) > len(first_sequences)
        user_messages = [str(event.payload["content"]) for event in events if event.event_type == "core.user_message"]
        assert user_messages == [first_prompt, second_prompt]
        # The durable tool result still carries the whole text the projection replaced.
        stored = json.dumps([dict(event.payload) for event in events if event.event_type == "core.tool_completed"], default=str)
        assert _NOTES_TEXT.splitlines()[-1] in stored
        assert any(event.event_type == "plugin.compaction.context_compacted" for event in events)

        # (d) The manual command reports the accounting the core holds, and no-ops after that.
        first = _command(core, session_id, "compact", {})
        assert first.status == "ok"
        assert first.data is not None
        assert first.data["covered_bytes"] == projection.covered_bytes
        assert first.data["dropped_bytes"] == projection.dropped_bytes
        second = _command(core, session_id, "compact", {})
        assert second.status == "ok"
        assert second.data is not None
        assert second.data["noop"] is True
        assert second.data["covered_bytes"] == core.store.window_projection(session_id).covered_bytes
        assert second.data["dropped_bytes"] == core.store.window_projection(session_id).dropped_bytes
        assert "Nothing new to compact" in (second.output or "")
        assert core.store.window_projection(session_id).covered_to_sequence == projection.covered_to_sequence

        # The substitution is exact: the next dispatch renders the projection row verbatim.
        in_force = core.store.window_projection(session_id)
        seen_requests = len(server.requests)
        assert core.run("third prompt: keep going", session_id=session_id).status == "completed"
        sent = next(request for request in server.requests[seen_requests:] if _summary_messages(request))
        assert _summary_messages(sent) == [
            f"[window summary by compaction; {in_force.covered_to_sequence} covered events "
            f"({in_force.covered_bytes} bytes -> {in_force.dropped_bytes} dropped)]\n{in_force.replacement}"
        ]


def test_manual_compact_projects_on_demand_then_noops(tmp_path: Path) -> None:
    # Usage stays under the trigger, so only the manual command can project this session.
    replies = [
        _turn(_tool_call("call-1", _WRITE_TOOL, {"path": "notes.txt", "text": _NOTES_TEXT}), prompt_tokens=10),
        _turn(output="notes written", prompt_tokens=10),
        openai_response(message={"role": "assistant", "content": "the manual probe notes"}, finish="stop"),
        _turn(output="second answer", prompt_tokens=10),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session("sess-manual").session_id
        first_prompt = "first prompt: start the probe manually"
        assert core.run(first_prompt, session_id=session_id).status == "completed"
        # Below the trigger: the automatic lane never projected anything.
        assert core.store.window_projection(session_id) is None
        assert _summary_messages(server.requests[-1]) == []

        projected = _command(core, session_id, "compact", {})
        assert projected.status == "ok"
        assert projected.data is not None
        assert projected.data["noop"] is False
        assert projected.data["summary_kind"] == "model"
        row = core.store.window_projection(session_id)
        assert row is not None
        assert "the manual probe notes" in row.replacement
        assert row.owner == "compaction"
        assert projected.data["covered_bytes"] == row.covered_bytes
        assert projected.data["dropped_bytes"] == row.dropped_bytes
        assert projected.data["covered_from_sequence"] == row.covered_from_sequence

        noop = _command(core, session_id, "compact", {})
        assert noop.status == "ok"
        assert noop.data is not None
        assert noop.data["noop"] is True
        assert noop.data["covered_bytes"] == row.covered_bytes
        assert noop.data["dropped_bytes"] == row.dropped_bytes

        # The durable state is this plugin's own bookkeeping, and it satisfies the schema the
        # registration declares (the core validates owner state against exactly that contract).
        stored, version = core.store.plugin_state("compaction", "session", session_id)
        assert version >= 1
        assert isinstance(stored, Mapping)
        from voidcode.plugins.compaction import create_plugin as create_compaction_plugin

        state_schema = create_compaction_plugin(None).state.schema
        assert state_schema is not None
        # Exactly how the core validates an owner's state copy against the declaration.
        jsonschema.Draft202012Validator(json_wire_object(state_schema)).validate(json_wire_object(stored))
        assert stored["schema_version"] == 2
        assert stored["cursor_sequence"] >= row.covered_to_sequence
        assert stored["last_projection"]["covered_bytes"] == row.covered_bytes
        assert stored["last_projection"]["dropped_bytes"] == row.dropped_bytes
        assert stored["last_projection"]["summary_kind"] == "model"

        # The declared projection is what the next dispatch actually sees.
        assert core.run("second prompt: ask again", session_id=session_id).status == "completed"
        summaries = _summary_messages(server.requests[-1])
        assert len(summaries) == 1
        assert f"({row.covered_bytes} bytes -> {row.dropped_bytes} dropped)]" in summaries[0]
        assert first_prompt not in [str(message.get("content", "")) for message in _messages(server.requests[-1]) if message.get("role") == "user"]
        events = core.events_after(session_id)
        assert any(str(event.payload.get("content", "")) == first_prompt for event in events if event.event_type == "core.user_message")


def test_manual_compact_fact_survives_reopen_and_delivers(tmp_path: Path) -> None:
    replies = [
        _turn(_tool_call("call-1", _WRITE_TOOL, {"path": "notes.txt", "text": _NOTES_TEXT}), prompt_tokens=10),
        _turn(output="notes written", prompt_tokens=10),
        openai_response(message={"role": "assistant", "content": "manual summary"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, observer=True)
        session_id = core.create_session("sess-compact-reopen").session_id
        assert core.run("first prompt: project before reopen", session_id=session_id).status == "completed"
        projected = _command(core, session_id, "compact", {})
        assert projected.status == "ok"
        assert projected.data is not None and projected.data["noop"] is False
        projection_event = next(event for event in core.events_after(session_id) if event.event_type == "core.window_projected")
        assert projection_event.payload["eligible_owners"] == ("projection-observer",)
        live_state, _ = core.store.plugin_state("projection-observer", "session", session_id)
        assert live_state is not None and live_state["observed_sequence"] == projection_event.sequence
        core.close()
        discard_core(core)

        reopened = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
        try:
            assert reopened.store.window_projection(session_id) is not None
            assert reopened.store.has_observer_receipt(session_id, projection_event.sequence, "projection-observer")
            replayed_state, _ = reopened.store.plugin_state("projection-observer", "session", session_id)
            assert replayed_state is not None and replayed_state["observed_sequence"] == projection_event.sequence
        finally:
            reopened.close()


def test_a_blank_model_summary_fails_loudly_and_stages_nothing(tmp_path: Path) -> None:
    replies = [
        _turn(_tool_call("call-1", _WRITE_TOOL, {"path": "notes.txt", "text": _NOTES_TEXT}), prompt_tokens=10),
        _turn(output="notes written", prompt_tokens=10),
        openai_response(message={"role": "assistant", "content": "   \n  "}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session("sess-blank").session_id
        assert core.run("first prompt: start the probe", session_id=session_id).status == "completed"
        assert core.store.window_projection(session_id) is None

        failed = _command(core, session_id, "compact", {})
        assert failed.status == "error"
        assert "blank" in (failed.error or "")
        assert core.store.window_projection(session_id) is None
        stored, _ = core.store.plugin_state("compaction", "session", session_id)
        assert stored is None
        assert not [event for event in core.events_after(session_id) if event.event_type == "plugin.compaction.context_compacted"]


def test_disable_and_reenable_clears_the_parked_coverage_and_reprojects(tmp_path: Path) -> None:
    replies = [
        _turn(_tool_call("call-1", _WRITE_TOOL, {"path": "notes.txt", "text": _NOTES_TEXT}), prompt_tokens=10),
        _turn(output="notes written", prompt_tokens=10),
        openai_response(message={"role": "assistant", "content": "the probe notes"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "the probe notes after re-enable"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        workspace = tmp_path / "workspace"
        config_path = workspace / "voidcode.json"
        session_id = core.create_session("sess-reenable").session_id
        assert core.run("first prompt: write the notes", session_id=session_id).status == "completed"
        projected = _command(core, session_id, "compact", {})
        assert projected.status == "ok", projected
        assert projected.data is not None and projected.data["noop"] is False
        assert core.store.window_projection(session_id) is not None

        # Removing the owner lets the activation transaction delete the projection row. The
        # parked state must stop describing that row, or a re-enable would wait for a new
        # provider-visible event before it could project again.
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["plugins"]["compaction"]["enabled"] = False
        config_path.write_text(json.dumps(config), encoding="utf-8")
        disabled = core.invoke_command(session_id, "core.reload", {}, expected_revision=core.command_catalog().revision_id)
        assert disabled.status == "ok", disabled
        assert core.store.window_projection(session_id) is None
        parked, _ = core.store.plugin_state("compaction", "session", session_id)
        assert isinstance(parked, Mapping)
        assert parked["last_projection"] is None
        assert parked["cursor_sequence"] >= 1

        config["plugins"]["compaction"]["enabled"] = True
        config_path.write_text(json.dumps(config), encoding="utf-8")
        enabled = core.invoke_command(session_id, "core.reload", {}, expected_revision=core.command_catalog().revision_id)
        assert enabled.status == "ok", enabled
        assert core.store.window_projection(session_id) is None

        # No new turn: the cleared coverage is what makes this immediate re-projection legal.
        reprojected = _command(core, session_id, "compact", {})
        assert reprojected.status == "ok", reprojected
        assert reprojected.data is not None
        assert reprojected.data["noop"] is False
        row = core.store.window_projection(session_id)
        assert row is not None
        assert "the probe notes after re-enable" in row.replacement
