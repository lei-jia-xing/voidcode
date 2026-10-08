from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from voidcode.acp.stdio import StdioAcpServer
from voidcode.runtime.active_session import ActiveRunInterruptResult
from voidcode.runtime.contracts import RuntimeRequest, RuntimeStreamChunk
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionRef, SessionState


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
        return ActiveRunInterruptResult(interrupted=True, session_id=session_id, run_id=run_id)


def test_acp_same_name_tool_calls_correlate_distinct_canonical_ids() -> None:
    session = SessionState(session=SessionRef(id="session-1"), status="running")
    chunks = [
        RuntimeStreamChunk(kind="control", session=session),
        # First tool call: read call-1
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="graph.tool_request_created",
                source="graph",
                sequence=1,
                session_id="session-1",
                payload={"tool": "read", "tool_call_id": "call-1", "arguments": {"path": "a.txt"}},
            ),
        ),
        # Second tool call with SAME NAME: read call-2
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="graph.tool_request_created",
                source="graph",
                sequence=2,
                session_id="session-1",
                payload={"tool": "read", "tool_call_id": "call-2", "arguments": {"path": "b.txt"}},
            ),
        ),
        # Complete call-1
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
        # Complete call-2
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="runtime.tool_completed",
                source="tool",
                sequence=4,
                session_id="session-1",
                payload={"tool": "read", "tool_call_id": "call-2", "status": "error", "error": "file not found"},
            ),
        ),
        RuntimeStreamChunk(kind="output", output="done", session=session),
    ]

    server = StdioAcpServer(runtime=FakeAcpRuntime(chunks), workspace=Path("/tmp"))
    new_res = server._new_session_result()
    acp_session_id = str(new_res["sessionId"])

    stdout = io.StringIO()
    server.stdout = stdout

    # Execute prompt
    server._handle_session_prompt(1, acp_session_id, "test prompt", server._sessions[acp_session_id])

    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    updates = [msg["params"]["update"] for msg in lines if msg.get("method") == "session/update"]

    # Collect tool events
    tool_calls = [u for u in updates if u.get("sessionUpdate") == "tool_call"]
    tool_updates = [u for u in updates if u.get("sessionUpdate") == "tool_call_update"]

    assert len(tool_calls) == 2
    assert tool_calls[0]["toolCallId"] == "call-1"
    assert tool_calls[0]["title"] == "read"
    assert tool_calls[1]["toolCallId"] == "call-2"
    assert tool_calls[1]["title"] == "read"

    assert len(tool_updates) == 2
    assert tool_updates[0]["toolCallId"] == "call-1"
    assert tool_updates[0]["status"] == "completed"
    assert tool_updates[1]["toolCallId"] == "call-2"
    assert tool_updates[1]["status"] == "failed"


def test_acp_tool_event_missing_canonical_id_fails_explicitly() -> None:
    server = StdioAcpServer(runtime=FakeAcpRuntime([]), workspace=Path("/tmp"))

    # Missing tool_call_id on request created
    req_event = EventEnvelope(
        event_type="graph.tool_request_created",
        source="graph",
        sequence=1,
        session_id="s1",
        payload={"tool": "read"},
    )
    with pytest.raises(ValueError, match="event payload missing canonical tool_call_id"):
        server._write_runtime_event_update(acp_session_id="acp-1", event=req_event)

    # Missing tool_call_id on completed
    comp_event = EventEnvelope(
        event_type="runtime.tool_completed",
        source="tool",
        sequence=2,
        session_id="s1",
        payload={"tool": "read"},
    )
    with pytest.raises(ValueError, match="event payload missing canonical tool_call_id"):
        server._write_runtime_event_update(acp_session_id="acp-1", event=comp_event)

    # Invalid status raises ValueError
    invalid_status_event = EventEnvelope(
        event_type="runtime.tool_completed",
        source="tool",
        sequence=3,
        session_id="s1",
        payload={"tool": "read", "tool_call_id": "call-1", "status": "unknown_status"},
    )
    with pytest.raises(ValueError, match="unsupported or missing tool completion status"):
        server._write_runtime_event_update(acp_session_id="acp-1", event=invalid_status_event)


def test_acp_stdio_server_full_jsonrpc_smoke() -> None:
    session = SessionState(session=SessionRef(id="session-smoke"), status="running")
    chunks = [
        RuntimeStreamChunk(kind="control", session=session),
        RuntimeStreamChunk(
            kind="control",
            session=session,
            event=EventEnvelope(
                event_type="graph.tool_request_created",
                source="graph",
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
    assert tool_calls[0]["toolCallId"] == "call-write-1"
    assert len(tool_updates) == 1
    assert tool_updates[0]["toolCallId"] == "call-write-1"
