"""A cancelled tool's own stop observation is durable before its run is terminated."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tests.fixtures.core_factory import loopback_server, open_core_for_test, write_loopback_config
from tests.fixtures.polling import wait_for
from tests.provider_loopback import openai_response
from voidcode.core.engine import CoreResult
from voidcode.core.events import EventType
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ConfirmedStop, ToolFailure, UnconfirmedStop

PLUGIN_ID = "tool-stop-fixture"

_PLUGIN_TEMPLATE = '''"""Fixture tools that return their own execution stop observation."""

from __future__ import annotations

import time

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import __OBSERVATION__, TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess

_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


class StopProbe:
    definition = ToolDefinition(
        name="stop_probe",
        description="Block until cancelled, then report the tool's own stop observation.",
        input_schema=_EMPTY_SCHEMA,
        effects=frozenset({ToolEffect.WRITE}),
    )

    def invoke(self, call, *, context, services):
        _ = services
        (context.workspace / "stop.started").touch()
        while not context.abort_signal.cancelled:
            time.sleep(0.01)
        return ToolFailure(
            call.tool_name,
            "the tool observed its own stop",
            execution=__OBSERVATION__(cancellation_signalled=True),
        )


class NeverRun:
    definition = ToolDefinition(
        name="never_run",
        description="Must never execute after the preceding batch member is interrupted.",
        input_schema=_EMPTY_SCHEMA,
        effects=frozenset({ToolEffect.WRITE}),
    )

    def invoke(self, call, *, context, services):
        (context.workspace / "never.ran").touch()
        return ToolSuccess(call.tool_name, TextOutput("unexpected"))


def create_plugin(config):
    return Plugin(tools=(StopProbe(), NeverRun()))
'''


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "stop commit reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _write_plugin(source: Path, observation: type[ConfirmedStop] | type[UnconfirmedStop]) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(_PLUGIN_TEMPLATE.replace("__OBSERVATION__", observation.__name__), encoding="utf-8")


@pytest.mark.parametrize("observation", [ConfirmedStop, UnconfirmedStop], ids=["confirmed", "unconfirmed"])
def test_cancelled_tool_stop_observation_commits_before_run_stop(tmp_path: Path, observation: type[ConfirmedStop] | type[UnconfirmedStop]) -> None:
    source = tmp_path / "plugin"
    _write_plugin(source, observation)
    replies = [
        _turn(_call("stop-1", "stop_probe"), _call("never-1", "never_run")),
        _turn(output="unexpected continuation after the cancelled batch"),
    ]
    with loopback_server(replies) as server:
        workspace = tmp_path / "workspace"
        write_loopback_config(workspace, server, source, plugin_id=PLUGIN_ID)
        core = open_core_for_test(tmp_path)
        core.create_session("s")

        outcome: dict[str, object] = {}

        def drive() -> None:
            outcome["result"] = core.run("stop the batch", session_id="s")

        worker = threading.Thread(target=drive, daemon=True)
        worker.start()
        wait_for(lambda: (workspace / "stop.started").is_file(), what="the stop-observation tool to start")
        assert core.cancel_session("s", "test cancelled the run") is True
        worker.join(timeout=30)
        assert not worker.is_alive()

        result = outcome.get("result")
        assert isinstance(result, CoreResult)
        assert result.status == "interrupted"
        assert result.stop_cause == "user_stop"
        events = list(core.store.events("s"))
        run_stop = next(event for event in events if event.event_type == EventType.RUN_INTERRUPTED)
        assert run_stop.payload["stop_cause"] == "user_stop"

        first = [
            event
            for event in events
            if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED}
            and event.payload.get("call", {}).get("tool_call_id") == "stop-1"
        ]
        assert len(first) == 1
        assert first[0].event_type == EventType.TOOL_INTERRUPTED
        assert first[0].sequence < run_stop.sequence
        decoded = decode_tool_result(first[0].payload["result"])
        assert isinstance(decoded, ToolFailure)
        assert type(decoded.execution) is observation
        assert decoded.execution.cancellation_signalled is True
        assert decoded.execution.execution_stopped is (observation is ConfirmedStop)
        assert decoded.execution.side_effect_state == ("settled" if observation is ConfirmedStop else "unknown")

        remaining = [
            event
            for event in events
            if event.event_type in {EventType.TOOL_COMPLETED, EventType.TOOL_INTERRUPTED}
            and event.payload.get("call", {}).get("tool_call_id") == "never-1"
        ]
        assert len(remaining) == 1
        assert remaining[0].event_type == EventType.TOOL_INTERRUPTED
        remaining_result = decode_tool_result(remaining[0].payload["result"])
        assert isinstance(remaining_result, ToolFailure)
        assert "not executed" in remaining_result.error
        assert remaining_result.execution is None
        assert not (workspace / "never.ran").exists()
