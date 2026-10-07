from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest

from voidcode.core.transcript import ToolResultView
from voidcode.core.turns import FinalTurn, ToolTurn, TurnRequest, TurnSession
from voidcode.provider.config import OpenAICompatibleProviderConfig, ProviderConfigs
from voidcode.runtime.composition import CompositionRef, FrozenComposition
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.storage.ports import RuntimeRepositories
from voidcode.tools.contracts import ToolCall
from voidcode.tools.read import ReadTool
from voidcode.tools.write import WriteTool


class ReadThenWriteProducer:
    def __init__(self) -> None:
        self.read_context: str | None = None

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        _ = session
        if not tool_results:
            return ToolTurn((ToolCall("read", {"path": "sample.txt"}),))
        if len(tool_results) == 1:
            read_segment = next(segment for segment in request.assembled_context.segments if segment.role == "tool" and segment.tool_name == "read")
            self.read_context = read_segment.content
            if self.read_context is None or "before" not in self.read_context:
                raise AssertionError("the model-facing read result omitted the original file content")
            hash_match = re.search(r"SHA-256 content hash: ([0-9a-f]{64})", self.read_context)
            if hash_match is None:
                raise AssertionError("the model-facing read result omitted the content hash")
            return ToolTurn(
                (
                    ToolCall(
                        "write",
                        {"path": "sample.txt", "content": "after\n", "expectedHash": hash_match.group(1)},
                    ),
                )
            )
        return FinalTurn("read then write completed")


def test_runtime_reads_before_writing_and_replays_the_persisted_read_pair(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("before\n", encoding="utf-8")
    producer = ReadThenWriteProducer()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool(), WriteTool()]),
        turn_producer=producer,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False), execution_engine="deterministic", approval_mode="yolo"),
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    with runtime:
        response = runtime.run(RuntimeRequest(prompt="read and replace sample.txt", session_id="runtime-read-write"))
        completed = [event for event in response.events if event.event_type == "runtime.tool_completed"]
        read_event = completed[0]
        assert source.read_text(encoding="utf-8") == "after\n"
        assert response.session.status == "completed"
        assert producer.read_context is not None and "before" in producer.read_context
        replayed = runtime.replayed_conversation_segments_for_existing_session(
            stored=response,
            parent_session_id=None,
        )
        read_call = next(segment for segment in replayed if segment.role == "assistant" and segment.tool_name == "read")
        read_result = next(segment for segment in replayed if segment.role == "tool" and segment.tool_name == "read")
        assert read_call.tool_call_id == read_event.payload["tool_call_id"] == read_result.tool_call_id
        assert read_call.tool_arguments == {"path": "sample.txt"}
        assert read_result.content is not None and "before" in read_result.content


class _OpenAILoopbackServer(ThreadingHTTPServer):
    def __init__(self, replies: list[dict[str, object]]) -> None:
        super().__init__(("127.0.0.1", 0), _OpenAILoopbackHandler)
        self.replies = replies
        self.requests: list[dict[str, object]] = []


class _OpenAILoopbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(_OpenAILoopbackServer, self.server)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.requests.append(payload)
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        response = server.replies.pop(0)
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


def _openai_response(*, message: dict[str, object], finish: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-loopback",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


def test_openai_sdk_resumes_original_batch_after_read_before_approved_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    source = tmp_path / "sample.txt"
    source.write_text("before\n", encoding="utf-8")
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    read_id = "call_read_native_1"
    write_id = "call_write_native_1"
    reasoning_text = "Inspect the file, then perform the approved replacement."
    server = _OpenAILoopbackServer(
        [
            _openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning_text,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": read_id,
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"sample.txt"}'},
                        },
                        {
                            "index": 1,
                            "id": write_id,
                            "type": "function",
                            "function": {
                                "name": "write",
                                "arguments": json.dumps({"path": "sample.txt", "content": "after\n", "expectedHash": expected_hash}),
                            },
                        },
                    ],
                },
                finish="tool_calls",
            ),
            _openai_response(message={"role": "assistant", "content": "Done."}, finish="stop"),
        ]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def make_runtime(store: SqliteSessionStore) -> VoidCodeRuntime:
        repositories = RuntimeRepositories(store, store, store, store, store, store, store)
        return VoidCodeRuntime(
            workspace=tmp_path,
            repositories=repositories,
            config=RuntimeConfig(
                approval_mode="ask",
                execution_engine="provider",
                model="deepseek/deepseek-v4-pro",
                mcp=RuntimeMcpConfig(enabled=False),
                providers=ProviderConfigs(
                    deepseek=OpenAICompatibleProviderConfig(
                        api_key="loopback-key",
                        base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                        timeout_seconds=5,
                    )
                ),
            ),
            permission_policy=PermissionPolicy(mode="ask"),
        )

    store = SqliteSessionStore(database_path=tmp_path / "sdk-resume.sqlite3")
    runtime = make_runtime(store)
    session_id = "openai-loopback-batch-resume"
    try:
        with runtime:
            waiting = runtime.run(RuntimeRequest(prompt="read then write sample.txt", session_id=session_id))
            waiting_completed = [event for event in waiting.events if event.event_type == "runtime.tool_completed"]
            assert [event.payload["tool_call_id"] for event in waiting_completed] == [read_id], (
                waiting.session.status,
                [(event.event_type, dict(event.payload)) for event in waiting.events],
                server.requests,
            )
            approval = next(event for event in waiting.events if event.event_type == "runtime.approval_requested")
            assert source.read_text(encoding="utf-8") == "before\n"
            request_tools = server.requests[0]["tools"]
            assert isinstance(request_tools, list)
            requested_names = {
                tool["function"]["name"]
                for tool in request_tools
                if isinstance(tool, dict) and isinstance(tool.get("function"), dict) and isinstance(tool["function"].get("name"), str)
            }
            assert {"read", "write"} <= requested_names
            assert not any(event.event_type == "runtime.tool_started" and event.payload.get("tool_call_id") == write_id for event in waiting.events)

        new_store = SqliteSessionStore(database_path=tmp_path / "sdk-resume.sqlite3")
        new_runtime = make_runtime(new_store)
        with new_runtime:
            resumed = new_runtime.resume(
                session_id,
                approval_request_id=str(approval.payload["request_id"]),
                approval_decision="allow",
            )
            events = {event.sequence: event for event in (*waiting.events, *resumed.events)}
            completed = sorted(
                (event for event in events.values() if event.event_type == "runtime.tool_completed"),
                key=lambda event: event.sequence,
            )
            assert [event.payload["tool_call_id"] for event in completed] == [read_id, write_id]
            assert source.read_text(encoding="utf-8") == "after\n"
            assert resumed.session.status == "completed"
            assert len(server.requests) == 2
            messages = server.requests[1]["messages"]
            assert isinstance(messages, list)
            original_call_message = next(
                message for message in messages if isinstance(message, dict) and message.get("role") == "assistant" and message.get("tool_calls")
            )
            assert original_call_message["reasoning_content"] == reasoning_text
            read_results = [
                message
                for message in messages
                if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id") == read_id
            ]
            assert len(read_results) == 1
            assert "before" in str(read_results[0].get("content"))
            assert expected_hash in str(read_results[0].get("content"))
            fork = new_runtime.fork_session(session_id=session_id)
            stored_fork = new_store.load_session(workspace=tmp_path, session_id=fork.session.id)
            source = new_store.load_session(workspace=tmp_path, session_id=session_id)
            capability = stored_fork.session.metadata["agent_capability_snapshot"]
            assert isinstance(capability, dict)
            composition_ref = capability["composition_ref"]
            assert isinstance(composition_ref, dict)
            assert composition_ref == source.session.metadata["composition_ref"]
            assert "execution_composition" not in stored_fork.session.metadata
            assert new_store.load_execution_composition(ref=CompositionRef.model_validate(composition_ref)) == FrozenComposition.from_payload(
                source.session.metadata["execution_composition"]
            )
            fork_replay = new_runtime.resume(fork.session.id)
            assert fork_replay.session.session.id == fork.session.id
            assert len(server.requests) == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
