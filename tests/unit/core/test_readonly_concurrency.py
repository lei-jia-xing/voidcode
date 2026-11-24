"""Unit tests for concurrent read-only tool batch execution in TurnExecutionManager."""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.core_factory import loopback_server
from tests.fixtures.s5_plugin import PLUGIN_ID, PROBE_TOOL, write_s5_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.events import EventType


def _call(identity: str, tool_name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": list(calls)}, finish="tool_calls" if calls else "stop")


def _configure(tmp_path: Path, server: OpenAILoopbackServer) -> Path:
    source = write_s5_plugin(tmp_path / "source")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo", "read_only": False},
                "providers": {"endpoint": {"api_key": "secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": {PLUGIN_ID: {"path": str(source)}},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def test_readonly_tool_batch_runs_and_commits_in_strict_order(tmp_path: Path) -> None:
    calls = [
        _call("call_0", PROBE_TOOL, action="session_info"),
        _call("call_1", PROBE_TOOL, action="session_info"),
        _call("call_2", PROBE_TOOL, action="session_info"),
    ]
    with loopback_server([_turn(*calls), _turn(output="all probes finished")]) as server:
        workspace = _configure(tmp_path, server)
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            session = core.create_session("sess-batch-ro")
            result = core.run("run read-only batch", session_id=session.session_id)
            assert result.output == "all probes finished"
            assert result.status.value == "completed"

            # Check that events were committed in strict order 0, 1, 2
            completed_events = [ev for ev in core.store.events(session.session_id) if ev.event_type == EventType.TOOL_COMPLETED]
            assert len(completed_events) == 3
            ids = [ev.payload["call"]["tool_call_id"] for ev in completed_events]
            assert ids == ["call_0", "call_1", "call_2"]

            # Verify intents were committed in order before their completions
            all_events = list(core.store.events(session.session_id))
            tool_event_types = [
                (ev.event_type, ev.payload.get("call", {}).get("tool_call_id"))
                for ev in all_events
                if ev.event_type in (EventType.TOOL_INTENT, EventType.TOOL_COMPLETED)
            ]
            expected = [
                (EventType.TOOL_INTENT, "call_0"),
                (EventType.TOOL_COMPLETED, "call_0"),
                (EventType.TOOL_INTENT, "call_1"),
                (EventType.TOOL_COMPLETED, "call_1"),
                (EventType.TOOL_INTENT, "call_2"),
                (EventType.TOOL_COMPLETED, "call_2"),
            ]
            assert tool_event_types == expected


def test_stream_provider_turn_retries_before_first_byte(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from voidcode.core.engine.turn_execution import TurnDriver
    from voidcode.sdk.provider import ProviderStreamEvent, ProviderTurnRequest

    host = MagicMock()
    manager = TurnDriver(host)

    attempts_seen: list[int] = []

    def mock_stream_turn(req: ProviderTurnRequest, provider: str):
        attempts_seen.append(req.attempt or 1)
        if req.attempt == 1:
            yield ProviderStreamEvent(
                kind="error",
                error="rate limit (429)",
                error_kind="rate_limit",
                retryable=True,
                retry_after=0.01,
            )
        else:
            yield ProviderStreamEvent(kind="delta", channel="text", text="hello from retry")
            yield ProviderStreamEvent(kind="done", done_reason="stop")

    snapshot = MagicMock()
    snapshot.plugin_runtime.stream_turn = mock_stream_turn

    execution = MagicMock()
    execution.session.session_id = "sess-1"
    execution.run_id = "run-1"

    request = ProviderTurnRequest(
        messages=(),
        available_tools=(),
        raw_model="endpoint/model",
        provider_name="endpoint",
        model_name="model",
    )

    result = manager.stream_provider_turn(
        snapshot,
        execution,
        request,
        provider_name="endpoint",
        dispatch_id="disp-1",
        dispatch_sequence=10,
    )

    assert attempts_seen == [1, 2]
    assert result.output == "hello from retry"
    assert result.done_reason == "stop"


def test_stream_provider_turn_does_not_retry_after_bytes_flowed(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    import pytest

    from voidcode.core.engine.turn_execution import TurnDriver
    from voidcode.sdk.provider import ProviderExecutionError, ProviderStreamEvent, ProviderTurnRequest

    host = MagicMock()
    manager = TurnDriver(host)

    attempts_seen: list[int] = []

    def mock_stream_turn(req: ProviderTurnRequest, provider: str):
        attempts_seen.append(req.attempt or 1)
        yield ProviderStreamEvent(kind="delta", channel="text", text="partial data")
        yield ProviderStreamEvent(
            kind="error",
            error="connection dropped",
            error_kind="network_error",
            retryable=True,
            retry_after=0.01,
        )

    snapshot = MagicMock()
    snapshot.plugin_runtime.stream_turn = mock_stream_turn

    execution = MagicMock()
    execution.session.session_id = "sess-1"
    execution.run_id = "run-1"

    request = ProviderTurnRequest(
        messages=(),
        available_tools=(),
        raw_model="endpoint/model",
        provider_name="endpoint",
        model_name="model",
    )

    with pytest.raises(ProviderExecutionError) as exc_info:
        manager.stream_provider_turn(
            snapshot,
            execution,
            request,
            provider_name="endpoint",
            dispatch_id="disp-1",
            dispatch_sequence=10,
        )

    assert attempts_seen == [1]  # never retried after bytes flowed
    assert "connection dropped" in str(exc_info.value)
    assert exc_info.value.retryable is True
    assert exc_info.value.retry_after == 0.01
