from __future__ import annotations

import io
import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from voidcode.acp.stdio import StdioAcpServer
from voidcode.runtime.active_session import ActiveRunInterruptResult
from voidcode.runtime.contracts import RuntimeRequest, RuntimeStreamChunk
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState, SessionStatus


class FakeAcpRuntime:
    def __init__(self, chunks: list[RuntimeStreamChunk]) -> None:
        self.chunks = chunks
        self.seen_requests: list[RuntimeRequest] = []

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        self.seen_requests.append(request)
        yield from self.chunks

    def cancel_session(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> ActiveRunInterruptResult:
        return ActiveRunInterruptResult(
            session_id=session_id,
            status="interrupted",
            run_id=run_id,
        )


def _run_prompt_with_status(status: SessionStatus, *, event: EventEnvelope | None = None) -> dict[str, object]:
    session = SessionState(session=SessionRef(id="session-outcome"), status=status)
    runtime = FakeAcpRuntime([RuntimeStreamChunk(kind="control", session=session, event=event)])
    server = StdioAcpServer(runtime=runtime, workspace=Path("/tmp"), stdout=io.StringIO())
    acp_session_id = str(server._new_session_result()["sessionId"])
    server._handle_session_prompt(1, acp_session_id, "test prompt", server._sessions[acp_session_id])
    return json.loads(server.stdout.getvalue().splitlines()[-1])


def test_acp_reused_native_call_id_uses_turn_scoped_activity_ids() -> None:
    session = SessionState(session=SessionRef(id="session-1"), status="completed")
    manifest = {
        "tool_batch_calls": [
            {
                "tool": "read",
                "tool_call_id": "call-1",
                "arguments": {"path": "a.txt"},
            }
        ]
    }
    chunks = [
        RuntimeStreamChunk(kind="control", session=session),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="core.model_turn",
                source="core",
                sequence=1,
                session_id="session-1",
                payload=manifest,
            ),
        ),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="core.tool_request_created",
                source="core",
                sequence=2,
                session_id="session-1",
                payload={"tool": "read", "tool_call_id": "call-1", "arguments": {"path": "a.txt"}},
            ),
        ),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="runtime.tool_completed",
                source="tool",
                sequence=3,
                session_id="session-1",
                payload={"tool": "read", "tool_call_id": "call-1", "status": "ok"},
            ),
        ),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="core.model_turn",
                source="core",
                sequence=4,
                session_id="session-1",
                payload=manifest,
            ),
        ),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="core.tool_request_created",
                source="core",
                sequence=5,
                session_id="session-1",
                payload={"tool": "read", "tool_call_id": "call-1", "arguments": {"path": "b.txt"}},
            ),
        ),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="runtime.tool_completed",
                source="tool",
                sequence=6,
                session_id="session-1",
                payload={"tool": "read", "tool_call_id": "call-1", "status": "error", "error": "file not found"},
            ),
        ),
        RuntimeStreamChunk(kind="output", output="done", session=session),
    ]

    server = StdioAcpServer(runtime=FakeAcpRuntime(chunks), workspace=Path("/tmp"))
    acp_session_id = str(server._new_session_result()["sessionId"])
    stdout = io.StringIO()
    server.stdout = stdout
    server._handle_session_prompt(1, acp_session_id, "test prompt", server._sessions[acp_session_id])

    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    updates = [msg["params"]["update"] for msg in lines if msg.get("method") == "session/update"]
    tool_calls = [update for update in updates if update.get("sessionUpdate") == "tool_call"]
    tool_updates = [update for update in updates if update.get("sessionUpdate") == "tool_call_update"]

    assert len(tool_calls) == 2
    first_id = tool_calls[0]["toolCallId"]
    second_id = tool_calls[1]["toolCallId"]
    assert first_id != "call-1"
    assert second_id != "call-1"
    assert first_id != second_id
    assert [update["toolCallId"] for update in tool_updates] == [first_id, second_id]
    assert [update["status"] for update in tool_updates] == ["completed", "failed"]
    assert "call-1" not in stdout.getvalue()


def test_acp_tool_event_missing_canonical_id_fails_explicitly() -> None:
    server = StdioAcpServer(runtime=FakeAcpRuntime([]), workspace=Path("/tmp"))

    # Missing tool_call_id on request created
    req_event = EventEnvelope(
        event_type="core.tool_request_created",
        source="core",
        sequence=1,
        session_id="s1",
        payload={"tool": "read"},
    )
    with pytest.raises(ValueError, match="event payload missing canonical tool_call_id"):
        server._write_runtime_event_update(
            acp_session_id="acp-1",
            event=req_event,
            tool_activity_ids={},
            pending_tool_activity_ids=set(),
        )

    # Missing tool_call_id on completed
    comp_event = EventEnvelope(
        event_type="runtime.tool_completed",
        source="tool",
        sequence=2,
        session_id="s1",
        payload={"tool": "read"},
    )
    with pytest.raises(ValueError, match="event payload missing canonical tool_call_id"):
        server._write_runtime_event_update(
            acp_session_id="acp-1",
            event=comp_event,
            tool_activity_ids={},
            pending_tool_activity_ids=set(),
        )

    # Invalid status raises ValueError
    invalid_status_event = EventEnvelope(
        event_type="runtime.tool_completed",
        source="tool",
        sequence=3,
        session_id="s1",
        payload={"tool": "read", "tool_call_id": "call-1", "status": "unknown_status"},
    )
    with pytest.raises(ValueError, match="unsupported or missing tool completion status"):
        server._write_runtime_event_update(
            acp_session_id="acp-1",
            event=invalid_status_event,
            tool_activity_ids={"call-1": "voidcode:s1:1:0"},
            pending_tool_activity_ids=set(),
        )


def test_acp_completed_session_maps_to_supported_end_turn() -> None:
    response = _run_prompt_with_status("completed")

    assert response["result"]["stopReason"] == "end_turn"


def test_acp_interrupted_session_maps_to_cancelled_even_without_binding_flag() -> None:
    event = EventEnvelope(
        event_type="runtime.failed",
        source="runtime",
        sequence=1,
        session_id="session-outcome",
        payload={"kind": "interrupted", "cancelled": True},
    )

    response = _run_prompt_with_status("interrupted", event=event)

    assert response["result"]["stopReason"] == "cancelled"


@pytest.mark.parametrize("status", ["running", "idle"])
def test_acp_nonterminal_eof_is_an_internal_outcome_error(status: SessionStatus) -> None:
    response = _run_prompt_with_status(status)

    assert response["error"]["code"] == -32603
    assert "outcome is unknown" in response["error"]["message"]


@pytest.mark.parametrize("event_type", ["runtime.approval_requested", "runtime.question_requested"])
def test_acp_waiting_for_approval_or_question_is_not_reported_as_turn_complete(event_type: str) -> None:
    event = EventEnvelope(
        event_type=event_type,
        source="runtime",
        sequence=1,
        session_id="session-outcome",
        payload={"request_id": "blocked-1"},
    )
    response = _run_prompt_with_status("waiting", event=event)

    assert response["error"]["code"] == -32603
    assert "approval or a question" in response["error"]["message"]


def test_acp_stdio_server_full_jsonrpc_smoke() -> None:
    session = SessionState(session=SessionRef(id="session-smoke"), status="completed")
    chunks = [
        RuntimeStreamChunk(kind="control", session=session),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="core.tool_request_created",
                source="core",
                sequence=1,
                session_id="session-smoke",
                payload={"tool": "write", "tool_call_id": "call-write-1", "arguments": {"path": "x.txt"}},
            ),
        ),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="runtime.tool_completed",
                source="tool",
                sequence=2,
                session_id="session-smoke",
                payload={"tool": "write", "tool_call_id": "call-write-1", "status": "ok"},
            ),
        ),
        RuntimeStreamChunk(kind="output", output="file written", session=session),
    ]

    input_lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}}),
    ]

    # Pre-generate server and run first two methods to get session id
    stdin_stream = io.StringIO("\n".join(input_lines) + "\n")
    stdout_stream = io.StringIO()
    server = StdioAcpServer(runtime=FakeAcpRuntime(chunks), workspace=Path("/tmp"), stdin=stdin_stream, stdout=stdout_stream)
    server.serve()

    out_lines = [json.loads(line) for line in stdout_stream.getvalue().splitlines() if line.strip()]
    assert out_lines[0]["id"] == 1
    assert out_lines[0]["result"]["agentInfo"]["name"] == "voidcode"

    assert out_lines[1]["id"] == 2
    acp_session_id = cast(str, out_lines[1]["result"]["sessionId"])
    assert acp_session_id.startswith("acp-session-")

    # Now send session/prompt
    prompt_input = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": acp_session_id, "prompt": "write x.txt"},
            }
        )
        + "\n"
    )

    server.stdin = io.StringIO(prompt_input)
    stdout_stream.truncate(0)
    stdout_stream.seek(0)
    server.serve()

    prompt_out = [json.loads(line) for line in stdout_stream.getvalue().splitlines() if line.strip()]

    # Find the result for request id 3
    results = [msg for msg in prompt_out if msg.get("id") == 3]
    assert len(results) == 1
    assert results[0]["result"]["stopReason"] == "end_turn"

    # Verify notifications
    notifications = [msg for msg in prompt_out if msg.get("method") == "session/update"]
    tool_calls = [n["params"]["update"] for n in notifications if n["params"]["update"].get("sessionUpdate") == "tool_call"]
    tool_updates = [n["params"]["update"] for n in notifications if n["params"]["update"].get("sessionUpdate") == "tool_call_update"]

    assert len(tool_calls) == 1
    assert tool_calls[0]["toolCallId"] != "call-write-1"
    assert len(tool_updates) == 1
    assert tool_updates[0]["toolCallId"] == tool_calls[0]["toolCallId"]
    assert "call-write-1" not in stdout_stream.getvalue()


class _CancellableAcpRuntime:
    def __init__(self, *, hold_identity: bool) -> None:
        self.started = threading.Event()
        self.release_identity = threading.Event()
        self.cancelled = threading.Event()
        if not hold_identity:
            self.release_identity.set()
        self.cancel_calls: list[tuple[str, str | None, str | None]] = []

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        self.started.set()
        if not self.release_identity.wait(timeout=5):
            raise RuntimeError("test did not release runtime identity")
        metadata = {"runtime_state": {"run_id": "run-cancel"}}
        running = SessionState(
            session=SessionRef(id="runtime-cancel"),
            status="running",
            metadata=metadata,
        )
        yield RuntimeStreamChunk(
            kind="control",
            session=running,
            event=EventEnvelope(
                event_type="runtime.request_received",
                source="runtime",
                sequence=1,
                session_id="runtime-cancel",
                payload={"prompt": request.prompt},
            ),
        )
        if not self.cancelled.wait(timeout=5):
            raise RuntimeError("ACP cancel was not forwarded to the runtime")
        interrupted = SessionState(
            session=SessionRef(id="runtime-cancel"),
            status="interrupted",
            metadata=metadata,
        )
        yield RuntimeStreamChunk(
            kind="control",
            session=interrupted,
            event=EventEnvelope(
                event_type="runtime.failed",
                source="runtime",
                sequence=2,
                session_id="runtime-cancel",
                payload={"kind": "interrupted", "cancelled": True, "error": "run interrupted"},
            ),
        )

    def cancel_session(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> ActiveRunInterruptResult:
        self.cancel_calls.append((session_id, run_id, reason))
        self.cancelled.set()
        return ActiveRunInterruptResult(
            session_id=session_id,
            status="interrupted",
            run_id=run_id,
        )


@pytest.mark.parametrize("hold_identity", [False, True])
def test_acp_jsonrpc_cancel_drains_runtime_interruption(hold_identity: bool) -> None:
    runtime = _CancellableAcpRuntime(hold_identity=hold_identity)
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    writer = os.fdopen(write_fd, "w", buffering=1, encoding="utf-8")
    stdout = io.StringIO()
    server = StdioAcpServer(
        runtime=runtime,
        workspace=Path("/tmp"),
        stdin=stdin,
        stdout=stdout,
    )
    server_thread = threading.Thread(target=server.serve, daemon=True)
    server_thread.start()
    try:
        writer.writelines(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {},
                    }
                )
                + "\n",
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/new",
                        "params": {},
                    }
                )
                + "\n",
            ]
        )
        writer.flush()
        deadline = time.monotonic() + 2
        while not server._sessions and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(server._sessions) == 1
        acp_session_id = next(iter(server._sessions))

        writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": acp_session_id,
                        "prompt": "continue",
                    },
                }
            )
            + "\n"
        )
        writer.flush()
        assert runtime.started.wait(timeout=2)
        binding = server._sessions[acp_session_id]
        if not hold_identity:
            deadline = time.monotonic() + 2
            while (binding.runtime_session_id is None or binding.active_run_id is None) and time.monotonic() < deadline:
                time.sleep(0.005)
            assert binding.runtime_session_id == "runtime-cancel"
            assert binding.active_run_id == "run-cancel"

        writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/cancel",
                    "params": {"sessionId": acp_session_id},
                }
            )
            + "\n"
        )
        writer.flush()
        deadline = time.monotonic() + 2
        cancel_response: dict[str, object] | None = None
        while cancel_response is None and time.monotonic() < deadline:
            for line in stdout.getvalue().splitlines():
                message = json.loads(line)
                if message.get("id") == 4:
                    cancel_response = message
                    break
            if cancel_response is None:
                time.sleep(0.005)
        assert cancel_response is not None
        if hold_identity:
            assert cancel_response["result"]["queued"] is True
            assert cancel_response["result"]["cancelled"] is False
            runtime.release_identity.set()
        else:
            assert cancel_response["result"]["cancelled"] is True
    finally:
        runtime.release_identity.set()
        runtime.cancelled.set()
        writer.close()
        server_thread.join(timeout=3)
    assert not server_thread.is_alive()

    prompt_response = next(json.loads(line) for line in stdout.getvalue().splitlines() if json.loads(line).get("id") == 3)
    assert prompt_response["result"]["stopReason"] == "cancelled"
    assert runtime.cancel_calls == [("runtime-cancel", "run-cancel", "acp session/cancel")]
