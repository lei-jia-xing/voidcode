from __future__ import annotations

import importlib
import io
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from voidcode.acp.stdio import StdioAcpServer
from voidcode.runtime.contracts import RuntimeRequest


@dataclass(frozen=True, slots=True)
class _StubEvent:
    session_id: str
    sequence: int
    event_type: str
    source: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _StubSessionRef:
    id: str


@dataclass(frozen=True, slots=True)
class _StubSession:
    session: _StubSessionRef
    status: str = "running"


@dataclass(frozen=True, slots=True)
class _StubChunk:
    kind: str
    session: _StubSession
    event: _StubEvent | None = None
    output: str | None = None


class _StubRuntime:
    def __init__(self, chunks: list[_StubChunk] | None = None, *, fail: bool = False) -> None:
        self.chunks = chunks or []
        self.fail = fail
        self.requests: list[RuntimeRequest] = []

    def run_stream(self, request: RuntimeRequest) -> Iterator[Any]:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("runtime exploded")
        print("runtime debug should go to stderr")
        yield from self.chunks

    def cancel_session(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, object]:
        _ = run_id
        return {
            "session_id": session_id,
            "status": "interrupted",
            "interrupted": True,
            "cancelled": True,
            "run_id": "run-1",
            "reason": reason,
        }


class _SlowRuntime:
    def run_stream(self, request: RuntimeRequest) -> Iterator[Any]:
        _ = request
        yield _StubChunk(
            kind="event",
            session=_StubSession(_StubSessionRef("runtime-1")),
            event=_StubEvent(
                session_id="runtime-1",
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": "hello"},
            ),
        )
        time.sleep(0.05)
        yield _StubChunk(
            kind="output",
            session=_StubSession(_StubSessionRef("runtime-1"), status="completed"),
            output="late output",
        )


def _request(method: str, request_id: int, params: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload)


def _notification(method: str, params: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    return json.dumps(payload)


def _run_server(runtime: Any, *lines: str) -> tuple[list[dict[str, Any]], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    server = StdioAcpServer(
        runtime=runtime,
        workspace=Path("/tmp/workspace"),
        stdin=io.StringIO("\n".join(lines) + "\n"),
        stdout=stdout,
        stderr=stderr,
    )

    assert server.serve() == 0

    return [json.loads(line) for line in stdout.getvalue().splitlines()], stderr.getvalue()


def test_initialize_returns_capabilities_and_agent_metadata() -> None:
    messages, _ = _run_server(_StubRuntime(), _request("initialize", 1))

    assert messages == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {},
                    "mcpCapabilities": {},
                },
                "agentInfo": {
                    "name": "voidcode",
                    "title": "VoidCode",
                    "version": messages[0]["result"]["agentInfo"]["version"],
                },
                "authMethods": [],
            },
        }
    ]


def test_session_new_returns_external_session_id() -> None:
    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(_StubRuntime(), _request("session/new", 1))

    result = messages[0]["result"]
    assert result["sessionId"] == "acp-session-abc"


def test_prompt_happy_path_emits_event_output_and_result() -> None:
    runtime = _StubRuntime(
        [
            _StubChunk(
                kind="event",
                session=_StubSession(_StubSessionRef("runtime-1")),
                event=_StubEvent(
                    session_id="runtime-1",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": "hello"},
                ),
            ),
            _StubChunk(
                kind="output",
                session=_StubSession(_StubSessionRef("runtime-1"), status="completed"),
                output="done",
            ),
        ]
    )
    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, stderr = _run_server(
            runtime,
            _request("session/new", 1),
            _request(
                "session/prompt",
                2,
                {"sessionId": "acp-session-abc", "prompt": [{"type": "text", "text": "hello"}]},
            ),
        )

    assert messages[1]["method"] == "session/update"
    assert messages[1]["params"]["sessionId"] == messages[0]["result"]["sessionId"]
    assert messages[1]["params"]["update"]["sessionUpdate"] == "agent_thought_chunk"
    assert messages[2]["params"] == {
        "sessionId": messages[0]["result"]["sessionId"],
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "done"},
        },
    }
    assert messages[3]["result"] == {"stopReason": "end_turn"}
    assert runtime.requests[-1].prompt == "hello"
    assert runtime.requests[-1].allocate_session_id is True
    assert "runtime debug should go to stderr" in stderr


def test_prompt_failure_emits_failure_notification_and_json_rpc_error() -> None:
    messages, _ = _run_server(
        _StubRuntime(fail=True),
        _request("session/new", 1),
        _request("session/prompt", 2, {"sessionId": "acp-session-missing", "prompt": "hello"}),
    )

    assert messages[1]["error"]["code"] == -32602

    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            _StubRuntime(fail=True),
            _request("session/new", 1),
            _request("session/prompt", 2, {"sessionId": "acp-session-abc", "prompt": "hello"}),
        )

    assert messages[1]["method"] == "session/update"
    assert messages[1]["params"]["update"] == {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Runtime failed."},
    }
    assert messages[2]["error"] == {"code": -32603, "message": "runtime execution failed"}


def test_prompt_runtime_failed_event_returns_json_rpc_error() -> None:
    runtime = _StubRuntime(
        [
            _StubChunk(
                kind="event",
                session=_StubSession(_StubSessionRef("runtime-1")),
                event=_StubEvent(
                    session_id="runtime-1",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": "hello"},
                ),
            ),
            _StubChunk(
                kind="event",
                session=_StubSession(_StubSessionRef("runtime-1"), status="failed"),
                event=_StubEvent(
                    session_id="runtime-1",
                    sequence=2,
                    event_type="runtime.failed",
                    source="runtime",
                    payload={"error": "permission denied for tool: write_file"},
                ),
            ),
        ]
    )
    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            runtime,
            _request("session/new", 1),
            _request("session/prompt", 2, {"sessionId": "acp-session-abc", "prompt": "hello"}),
        )

    assert messages[-1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32603, "message": "runtime execution failed"},
    }
    assert not any(message.get("result") == {"stopReason": "end_turn"} for message in messages)


def test_cancel_returns_limited_success_without_crashing() -> None:
    messages, _ = _run_server(
        _StubRuntime(),
        _request("session/new", 1),
        _request("session/cancel", 2, {"sessionId": "unknown"}),
    )
    assert messages[1]["error"]["code"] == -32602

    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            _StubRuntime(),
            _request("session/new", 1),
            _request("session/cancel", 2, {"sessionId": "acp-session-abc"}),
        )
    assert messages[1]["result"]["cancelled"] is False
    assert messages[1]["result"]["stopReason"] == "not_active"
    assert messages[1]["result"]["supported"] is False
    assert messages[1]["result"]["runtimeCancel"] is None


def test_cancel_delegates_to_runtime_when_runtime_session_exists() -> None:
    runtime = _StubRuntime(
        [
            _StubChunk(
                kind="event",
                session=_StubSession(_StubSessionRef("runtime-1")),
                event=_StubEvent(
                    session_id="runtime-1",
                    sequence=1,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={"prompt": "hello"},
                ),
            ),
        ]
    )
    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            runtime,
            _request("session/new", 1),
            _request("session/prompt", 2, {"sessionId": "acp-session-abc", "prompt": "hello"}),
            _request("session/cancel", 3, {"sessionId": "acp-session-abc"}),
        )

    cancel_result = messages[-1]["result"]
    assert cancel_result["cancelled"] is True
    assert cancel_result["supported"] is True
    assert cancel_result["runtimeCancel"]["session_id"] == "runtime-1"


def test_cancel_notification_writes_no_response() -> None:
    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            _StubRuntime(),
            _request("session/new", 1),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": "acp-session-abc"},
                }
            ),
        )

    assert len(messages) == 1
    assert messages[0]["result"]["sessionId"] == "acp-session-abc"


def test_notification_style_requests_write_no_result_or_error() -> None:
    messages, _ = _run_server(
        _StubRuntime(),
        _notification("initialize"),
        _notification("session/new"),
        _notification("unknown"),
    )

    assert messages == []


def test_malformed_request_id_is_rejected_without_side_effects() -> None:
    runtime = _StubRuntime()
    messages, _ = _run_server(
        runtime,
        json.dumps({"jsonrpc": "2.0", "id": True, "method": "session/new", "params": {}}),
        _request("session/prompt", 1, {"sessionId": "acp-session-any", "prompt": "hello"}),
    )

    assert messages[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": -32600,
            "message": "id must be a string, integer, null, or omitted",
        },
    }
    assert messages[1]["error"] == {
        "code": -32602,
        "message": "unknown ACP session id: acp-session-any",
    }
    assert runtime.requests == []


def test_cancel_request_during_prompt_returns_cancelled_stop_reason() -> None:
    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            _SlowRuntime(),
            _request("session/new", 1),
            _request(
                "session/prompt",
                2,
                {"sessionId": "acp-session-abc", "prompt": [{"type": "text", "text": "hello"}]},
            ),
            _request("session/cancel", 3, {"sessionId": "acp-session-abc"}),
        )

    results = [message["result"] for message in messages if isinstance(message.get("result"), dict)]
    assert {result["stopReason"] for result in results if "stopReason" in result} == {"cancelled"}
    assert not any(
        message.get("params", {}).get("update", {}).get("content", {}).get("text") == "late output"
        for message in messages
    )


def test_rejects_concurrent_prompt_across_sessions() -> None:
    uuid_values = [SimpleNamespace(hex="abc"), SimpleNamespace(hex="def")]
    with patch("voidcode.acp.stdio.uuid4", side_effect=uuid_values):
        messages, _ = _run_server(
            _SlowRuntime(),
            _request("session/new", 1),
            _request("session/new", 2),
            _request("session/prompt", 3, {"sessionId": "acp-session-abc", "prompt": "first"}),
            _request("session/prompt", 4, {"sessionId": "acp-session-def", "prompt": "second"}),
            _request("session/cancel", 5, {"sessionId": "acp-session-abc"}),
        )

    assert any(
        message.get("id") == 4
        and message.get("error")
        == {
            "code": -32602,
            "message": "another ACP prompt is already running",
        }
        for message in messages
    )


def test_tool_call_update_reuses_created_tool_call_id() -> None:
    runtime = _StubRuntime(
        [
            _StubChunk(
                kind="event",
                session=_StubSession(_StubSessionRef("runtime-1")),
                event=_StubEvent(
                    session_id="runtime-1",
                    sequence=4,
                    event_type="graph.tool_request_created",
                    source="graph",
                    payload={"tool": "read", "arguments": {"path": "README.md"}},
                ),
            ),
            _StubChunk(
                kind="event",
                session=_StubSession(_StubSessionRef("runtime-1")),
                event=_StubEvent(
                    session_id="runtime-1",
                    sequence=5,
                    event_type="runtime.tool_started",
                    source="runtime",
                    payload={"tool": "read"},
                ),
            ),
            _StubChunk(
                kind="event",
                session=_StubSession(_StubSessionRef("runtime-1")),
                event=_StubEvent(
                    session_id="runtime-1",
                    sequence=6,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={"tool": "read", "path": "README.md"},
                ),
            ),
        ]
    )
    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            runtime,
            _request("session/new", 1),
            _request("session/prompt", 2, {"sessionId": "acp-session-abc", "prompt": "hello"}),
        )

    updates = [message["params"]["update"] for message in messages if "params" in message]
    tool_call = next(update for update in updates if update["sessionUpdate"] == "tool_call")
    tool_update = next(
        update for update in updates if update["sessionUpdate"] == "tool_call_update"
    )
    assert tool_update["toolCallId"] == tool_call["toolCallId"]


def test_rejects_oversized_line_and_prompt() -> None:
    messages, _ = _run_server(_StubRuntime(), "x" * 1_048_577)
    assert messages[0]["error"]["code"] == -32600

    with patch("voidcode.acp.stdio.uuid4", return_value=SimpleNamespace(hex="abc")):
        messages, _ = _run_server(
            _StubRuntime(),
            _request("session/new", 1),
            _request("session/prompt", 2, {"sessionId": "acp-session-abc", "prompt": "x" * 65_537}),
        )
    assert messages[1]["error"] == {"code": -32602, "message": "params.prompt is too large"}


def test_invalid_protocol_inputs_return_json_rpc_errors() -> None:
    messages, _ = _run_server(
        _StubRuntime(),
        "not-json",
        "[]",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "missing", "params": []}),
        _request("unknown", 2),
    )

    assert [message["error"]["code"] for message in messages] == [-32700, -32600, -32602, -32601]


def test_cli_acp_loads_config_constructs_runtime_and_keeps_stdout_protocol_clean() -> None:
    cli = importlib.import_module("voidcode.cli")
    stdout = io.StringIO()
    stderr = io.StringIO()
    stdin = io.StringIO(_request("initialize", 1) + "\n")
    workspace = Path("/tmp/acp-workspace")
    config = SimpleNamespace(approval_mode="deny")
    runtime = _StubRuntime()

    with patch.object(
        cli, "load_runtime_config", autospec=True, return_value=config
    ) as config_mock:
        with patch.object(
            cli, "VoidCodeRuntime", autospec=True, return_value=runtime
        ) as runtime_mock:
            with (
                patch.object(cli.sys, "stdin", stdin),
                patch.object(cli.sys, "stdout", stdout),
                patch.object(cli.sys, "stderr", stderr),
            ):
                result = cli.main(
                    [
                        "acp",
                        "--workspace",
                        str(workspace),
                        "--approval-mode",
                        "deny",
                    ]
                )

    assert result == 0
    config_mock.assert_called_once_with(workspace, approval_mode="deny")
    runtime_mock.assert_called_once_with(workspace=workspace, config=config)
    assert [json.loads(line)["jsonrpc"] for line in stdout.getvalue().splitlines()] == ["2.0"]
    assert "EVENT" not in stdout.getvalue()
