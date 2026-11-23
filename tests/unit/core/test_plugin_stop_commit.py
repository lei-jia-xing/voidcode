"""A cancelled tool's own stop observation keeps its staged work through the runtime."""

from __future__ import annotations

from pathlib import Path

from voidcode.core.cancellation import Cancellation
from voidcode.core.plugins import PluginRuntime, PluginSource, ToolInvocationOutcome
from voidcode.core.storage.store import SqliteStore
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import ToolCall, ToolFailure, UnconfirmedStop

PLUGIN_ID = "stop-fixture"

_PLUGIN_SOURCE = '''"""Fixture tools that stage state/events and return an optional stop observation."""

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolDefinition, ToolEffect, ToolFailure, UnconfirmedStop

_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_EFFECTS = frozenset({ToolEffect.READ, ToolEffect.SESSION})


def _stage(services):
    _state, version = services.get_state("session")
    services.replace_state({"mapping": ["child-1"]}, version)
    services.stage_event("mapped", {"task_id": "child-1"})


class ObservedStop:
    definition = ToolDefinition(name="observed_stop", description="", input_schema=_SCHEMA, effects=_EFFECTS)

    def invoke(self, call, *, context, services):
        _stage(services)
        return ToolFailure(call.tool_name, "observed stop", execution=UnconfirmedStop(cancellation_signalled=True))


class PlainCancel:
    definition = ToolDefinition(name="plain_cancel", description="", input_schema=_SCHEMA, effects=_EFFECTS)

    def invoke(self, call, *, context, services):
        _stage(services)
        return ToolFailure(call.tool_name, "plain cancellation")


def create_plugin(config):
    return Plugin(tools=(ObservedStop(), PlainCancel()))
'''


def _runtime(tmp_path: Path) -> tuple[SqliteStore, PluginRuntime, Path]:
    source = tmp_path / "plugin"
    source.mkdir()
    (source / "__init__.py").write_text(_PLUGIN_SOURCE, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SqliteStore(tmp_path / "state" / "voidcode.db")
    runtime = PluginRuntime(
        (PluginSource(root=source, plugin_id=PLUGIN_ID, config={}),),
        workspace=workspace,
        revision_id="rev-stop",
        store=store,
    )
    return store, runtime, workspace


def _context(workspace: Path, abort: Cancellation) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-stop",
        actor="model",
        abort_signal=abort,
    )


def test_cancelled_observed_stop_preserves_staged_state_and_events(tmp_path: Path) -> None:
    abort = Cancellation()
    abort.set_cancelled(True, reason="test stop")
    store, runtime, workspace = _runtime(tmp_path)
    try:
        outcome = runtime.invoke(
            ToolCall(tool_name="observed_stop", arguments={}),
            context=_context(workspace, abort),
            return_outcome=True,
        )
    finally:
        runtime.close()
        store.close()

    assert isinstance(outcome, ToolInvocationOutcome)
    assert isinstance(outcome.result, ToolFailure)
    assert outcome.result.execution == UnconfirmedStop(cancellation_signalled=True)
    [staged] = outcome.staged_state
    assert staged.owner == PLUGIN_ID
    assert staged.scope == "session"
    assert staged.scope_key == "session-1"
    assert staged.payload == {"mapping": ("child-1",)}
    [event] = outcome.staged_events
    assert event.event_type == f"plugin.{PLUGIN_ID}.mapped"
    assert event.payload == {"task_id": "child-1"}


def test_cancelled_result_without_stop_observation_discards_staged_work(tmp_path: Path) -> None:
    abort = Cancellation()
    abort.set_cancelled(True, reason="test stop")
    store, runtime, workspace = _runtime(tmp_path)
    try:
        outcome = runtime.invoke(
            ToolCall(tool_name="plain_cancel", arguments={}),
            context=_context(workspace, abort),
            return_outcome=True,
        )
    finally:
        runtime.close()
        store.close()

    assert isinstance(outcome, ToolInvocationOutcome)
    assert isinstance(outcome.result, ToolFailure)
    assert outcome.result.execution is None
    assert outcome.staged_state == ()
    assert outcome.staged_events == ()
