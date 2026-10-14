from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.transcript import ToolResultView
from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnRequest, TurnSession
from voidcode.provider.config import OpenAICompatibleProviderConfig, ProviderConfigs
from voidcode.runtime.bundle import write_session_bundle
from voidcode.runtime.composition import CompositionRef, FrozenComposition
from voidcode.runtime.config.loading import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permissions.policy import PermissionPolicy
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
            return ToolTurn(
                (ToolCall("read", {"path": "sample.txt"}),),
                facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
            )
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
                ),
                facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
            )
        return FinalTurn(
            "read then write completed",
            facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
        )


def test_runtime_reads_before_writing_and_replays_the_persisted_read_pair(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("OPENAI_API_KEY=sk-test-secret-12345\nbefore 🚀\n", encoding="utf-8")
    producer = ReadThenWriteProducer()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool(), WriteTool()]),
        turn_producer=producer,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False), approval_mode="yolo"),
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

    # Cold reopen with a fresh runtime to verify durable checkpoint and replay integrity
    fresh_runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([ReadTool(), WriteTool()]),
        turn_producer=producer,
        config=RuntimeConfig(mcp=RuntimeMcpConfig(enabled=False), approval_mode="yolo"),
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    with fresh_runtime:
        reopened = fresh_runtime.replay_session(session_id="runtime-read-write")
        assert reopened.session.status == "completed"
        reopened_segments = fresh_runtime.replayed_conversation_segments_for_existing_session(
            stored=reopened,
            parent_session_id=None,
        )
        reopened_read_tool = next(segment for segment in reopened_segments if segment.role == "tool" and segment.tool_name == "read")
        assert "OPENAI_API_KEY=sk-test-secret-12345" in (reopened_read_tool.content or "")
        assert "before 🚀" in (reopened_read_tool.content or "")
        assert source.read_text(encoding="utf-8") == "after\n"


def test_replayed_native_history_keeps_reused_call_ids_per_model_turn_and_execution_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    source = tmp_path / "sample.txt"
    source.write_text("first-version\n", encoding="utf-8")
    first_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    second_hash = hashlib.sha256(b"second-version\n").hexdigest()

    def tool_turn(call_id: str, name: str, arguments: dict[str, str], reasoning: str):
        return openai_response(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": reasoning,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            finish="tool_calls",
        )

    responses = [
        tool_turn("read_1", "read", {"path": "sample.txt"}, "Read the first version."),
        tool_turn(
            "write_1",
            "write",
            {"path": "sample.txt", "content": "second-version\n", "expectedHash": first_hash},
            "Replace the first version.",
        ),
        tool_turn("read_1", "read", {"path": "sample.txt"}, "Read the second version."),
        openai_response(message={"role": "assistant", "content": "First cycle done."}, finish="stop"),
        tool_turn(
            "write_2",
            "write",
            {
                "path": "sample.txt",
                "content": "third-version\n",
                "expectedHash": second_hash,
            },
            "Update the file in the new cycle.",
        ),
        tool_turn("read_1", "read", {"path": "sample.txt"}, "Read the third version."),
        openai_response(message={"role": "assistant", "content": "Second cycle done."}, finish="stop"),
    ]
    server = OpenAILoopbackServer(responses)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = SqliteSessionStore(database_path=tmp_path / "reused-call-ids.sqlite3")
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        repositories=RuntimeRepositories(store, store, store, store, store, store, store),
        tool_registry=ToolRegistry.from_tools([ReadTool(), WriteTool()]),
        config=RuntimeConfig(
            approval_mode="yolo",
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
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    try:
        with runtime:
            first = runtime.run(RuntimeRequest(prompt="First user prompt: read and update sample.txt", session_id="reused-call-history"))
            second = runtime.run(RuntimeRequest(prompt="Second user prompt: read and update sample.txt", session_id="reused-call-history"))

        assert len(server.requests) == 7
        assert first.session.status == second.session.status == "completed"
        assert source.read_text(encoding="utf-8") == "third-version\n"
        messages = server.requests[-1]["messages"]
        assert isinstance(messages, list)
        read_calls: list[tuple[int, dict[str, object]]] = []
        call_order: list[tuple[int, str, str | None]] = []
        call_names: list[str | None] = []
        call_arguments: list[tuple[str, object]] = []
        read_results: list[tuple[int, dict[str, object]]] = []
        result_order: list[tuple[int, str]] = []
        user_indices: dict[str, int] = {}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "user":
                content = str(message.get("content"))
                for prompt in ("First user prompt:", "Second user prompt:"):
                    if prompt in content:
                        user_indices[prompt] = index
            if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str):
                call_id = message["tool_call_id"]
                result_order.append((index, call_id))
                if call_id == "read_1":
                    read_results.append((index, message))
            if message.get("role") != "assistant" or not isinstance(message.get("tool_calls"), list):
                continue
            for call in message["tool_calls"]:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    continue
                call_id = call["id"]
                function = call.get("function")
                name = function.get("name") if isinstance(function, dict) else None
                call_names.append(name if isinstance(name, str) else None)
                reasoning = message.get("reasoning_content")
                call_order.append((index, call_id, reasoning if isinstance(reasoning, str) else None))
                assert isinstance(function, dict)
                call_arguments.append((call_id, json.loads(str(function["arguments"]))))
                if call_id == "read_1":
                    read_calls.append((index, message))
                    assert name == "read"

        assert [call_id for _, call_id, _ in call_order] == ["read_1", "write_1", "read_1", "write_2", "read_1"]
        assert call_names == ["read", "write", "read", "write", "read"]
        assert call_arguments == [
            ("read_1", {"path": "sample.txt"}),
            ("write_1", {"path": "sample.txt", "content": "second-version\n", "expectedHash": first_hash}),
            ("read_1", {"path": "sample.txt"}),
            ("write_2", {"path": "sample.txt", "content": "third-version\n", "expectedHash": second_hash}),
            ("read_1", {"path": "sample.txt"}),
        ]
        assert [reasoning for _, _, reasoning in call_order] == [
            "Read the first version.",
            "Replace the first version.",
            "Read the second version.",
            "Update the file in the new cycle.",
            "Read the third version.",
        ]
        assert [call_id for _, call_id in result_order] == ["read_1", "write_1", "read_1", "write_2", "read_1"]
        assert len(read_calls) == len(read_results) == 3
        assert [
            version
            for (_, result), version in zip(read_results, ("first-version", "second-version", "third-version"), strict=True)
            if version in str(result.get("content"))
        ] == ["first-version", "second-version", "third-version"]
        call_indices = [index for index, _, _ in call_order]
        result_indices = [index for index, _ in result_order]
        assert all(call_index < result_index for call_index, result_index in zip(call_indices, result_indices, strict=True))
        assert user_indices["First user prompt:"] < call_indices[0] < user_indices["Second user prompt:"] < call_indices[3]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_local_cli_session_continues_with_its_canonical_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    (tmp_path / "sample.txt").write_text("sample\n", encoding="utf-8")
    written = tmp_path / "written.txt"
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "Inspect the existing sample file.",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_cli_read",
                        "type": "function",
                        "function": {"name": "read", "arguments": json.dumps({"path": "sample.txt"})},
                    }
                ],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "Read sample.txt."}, finish="stop"),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "Write the accepted content to a new file.",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_cli_write",
                        "type": "function",
                        "function": {"name": "write", "arguments": json.dumps({"path": "written.txt", "content": "accepted"})},
                    }
                ],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "Wrote written.txt."}, finish="stop"),
    ]
    server = OpenAILoopbackServer(replies)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        config=RuntimeConfig(
            approval_mode="yolo",
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
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    try:
        with runtime:
            first = runtime.run(RuntimeRequest(prompt="read sample.txt"))
            second = runtime.run(RuntimeRequest(prompt="write written.txt accepted"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert first.session.session.id == second.session.session.id == "local-cli-session"
    assert first.session.status == second.session.status == "completed"
    assert written.read_text(encoding="utf-8") == "accepted"
    assert any(event.event_type == "runtime.tool_completed" and event.payload.get("tool_call_id") == "call_cli_read" for event in first.events)
    assert any(event.event_type == "runtime.tool_completed" and event.payload.get("tool_call_id") == "call_cli_write" for event in second.events)
    assert any(event.event_type == "runtime.approval_resolved" and event.payload.get("decision") == "allow" for event in second.events)


@pytest.mark.parametrize("separate_turns", [False, True], ids=("same-batch", "separate-turns"))
def test_openai_sdk_preserves_read_reasoning_across_approved_write_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    separate_turns: bool,
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
    write_reasoning_text = "The approved write has this separate reasoning turn."
    read_tool_call = {
        "index": 0,
        "id": read_id,
        "type": "function",
        "function": {"name": "read", "arguments": '{"path":"sample.txt"}'},
    }
    write_tool_call = {
        "index": 1,
        "id": write_id,
        "type": "function",
        "function": {
            "name": "write",
            "arguments": json.dumps({"path": "sample.txt", "content": "after\n", "expectedHash": expected_hash}),
        },
    }
    responses = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": reasoning_text,
                "tool_calls": [read_tool_call] if separate_turns else [read_tool_call, write_tool_call],
            },
            finish="tool_calls",
        )
    ]
    if separate_turns:
        responses.append(
            openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": write_reasoning_text,
                    "tool_calls": [{**write_tool_call, "index": 0}],
                },
                finish="tool_calls",
            )
        )
    responses.append(openai_response(message={"role": "assistant", "content": "Done."}, finish="stop"))
    server = OpenAILoopbackServer(responses)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def make_runtime(store: SqliteSessionStore) -> VoidCodeRuntime:
        repositories = RuntimeRepositories(store, store, store, store, store, store, store)
        return VoidCodeRuntime(
            workspace=tmp_path,
            repositories=repositories,
            config=RuntimeConfig(
                approval_mode="ask",
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
            approvals = [event for event in waiting.events if event.event_type == "runtime.approval_requested"]
            assert approvals, (
                waiting.session.status,
                [
                    (event.payload.get("error"), event.payload.get("provider_error_details"))
                    for event in waiting.events
                    if event.event_type == "runtime.failed"
                ],
                [
                    (event.event_type, event.sequence, event.payload.get("text"), event.payload.get("tool_call_id"))
                    for event in waiting.events
                    if event.event_type
                    in {
                        "runtime.request_received",
                        "runtime.reasoning_part",
                        "core.model_turn",
                        "core.tool_request_created",
                        "runtime.tool_completed",
                    }
                ],
                [
                    (
                        segment.role,
                        segment.tool_call_id,
                        segment.metadata.get("kind"),
                        segment.metadata.get("data"),
                    )
                    for segment in runtime.replayed_conversation_segments_for_existing_session(
                        stored=waiting,
                        parent_session_id=None,
                    )
                ],
            )
            approval = approvals[0]
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
            assert resumed.session.status == "completed", [
                (event.payload.get("error"), event.payload.get("provider_error_details"))
                for event in resumed.events
                if event.event_type == "runtime.failed"
            ]
            messages = server.requests[-1]["messages"]
            assert isinstance(messages, list)

            def assistant_message_for(tool_call_id: str) -> dict[str, object]:
                for message in messages:
                    if not isinstance(message, dict) or message.get("role") != "assistant":
                        continue
                    tool_calls = message.get("tool_calls")
                    if isinstance(tool_calls, list) and any(
                        isinstance(tool_call, dict) and tool_call.get("id") == tool_call_id for tool_call in tool_calls
                    ):
                        return message
                raise AssertionError(f"missing assistant call for {tool_call_id}")

            read_message = assistant_message_for(read_id)
            write_message = assistant_message_for(write_id)
            read_calls = read_message["tool_calls"]
            write_calls = write_message["tool_calls"]
            assert isinstance(read_calls, list) and isinstance(write_calls, list)
            wire_summary = [
                (
                    message.get("role"),
                    [call.get("id") for call in message.get("tool_calls", []) if isinstance(call, dict)]
                    if isinstance(message.get("tool_calls"), list)
                    else [],
                    message.get("reasoning_content"),
                    message.get("tool_call_id"),
                )
                for message in messages
                if isinstance(message, dict) and message.get("role") in {"assistant", "tool"}
            ]
            if separate_turns:
                assert read_message is not write_message
                assert [call["id"] for call in read_calls if isinstance(call, dict)] == [read_id]
                assert [call["id"] for call in write_calls if isinstance(call, dict)] == [write_id]
                assert read_message["reasoning_content"] == reasoning_text, wire_summary
                assert write_message["reasoning_content"] == write_reasoning_text, wire_summary
            else:
                assert read_message is write_message
                assert [call["id"] for call in read_calls if isinstance(call, dict)] == [read_id, write_id]
                assert read_message["reasoning_content"] == reasoning_text
            read_results = [
                message
                for message in messages
                if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id") == read_id
            ]
            write_results = [
                message
                for message in messages
                if isinstance(message, dict) and message.get("role") == "tool" and message.get("tool_call_id") == write_id
            ]
            assert len(read_results) == len(write_results) == 1
            assert "before" in str(read_results[0].get("content"))
            assert expected_hash in str(read_results[0].get("content"))
            read_result = json.loads(str(read_results[0]["content"]))
            write_result = json.loads(str(write_results[0]["content"]))
            assert read_result["status"] == write_result["status"] == "ok"
            assert read_result["error"] is None and write_result["error"] is None
            assert write_result["content"]
            tool_results = [message for message in messages if isinstance(message, dict) and message.get("role") == "tool"]
            assert [message["tool_call_id"] for message in tool_results] == [read_id, write_id]
            timeline = [
                (
                    index,
                    message.get("role"),
                    message.get("content"),
                    [call.get("id") for call in message.get("tool_calls", []) if isinstance(call, dict)]
                    if isinstance(message.get("tool_calls"), list)
                    else [],
                    message.get("tool_call_id"),
                )
                for index, message in enumerate(messages)
                if isinstance(message, dict)
            ]
            user_prompt_index = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if isinstance(message, dict) and message.get("role") == "user" and "read then write sample.txt" in str(message.get("content"))
                ),
                None,
            )
            read_message_index = next(index for index, message in enumerate(messages) if message is read_message)
            write_message_index = next(index for index, message in enumerate(messages) if message is write_message)
            read_result_index = next(index for index, message in enumerate(messages) if message is read_results[0])
            write_result_index = next(index for index, message in enumerate(messages) if message is write_results[0])
            assert user_prompt_index is not None, timeline
            if separate_turns:
                assert user_prompt_index < read_message_index < read_result_index < write_message_index < write_result_index, timeline
            else:
                assert user_prompt_index < read_message_index == write_message_index < read_result_index < write_result_index, timeline
            fork = new_runtime.fork_session(session_id=session_id)
            stored_fork = new_store.load_session(workspace=tmp_path, session_id=fork.session.id)
            source = new_store.load_session(workspace=tmp_path, session_id=session_id)
            capability = stored_fork.session.metadata["agent_capability_snapshot"]
            assert isinstance(capability, dict)
            composition_ref = capability["composition_ref"]
            assert isinstance(composition_ref, dict)
            source_ref = source.session.metadata["composition_ref"]
            assert isinstance(source_ref, dict)
            assert composition_ref["owner"] == {"kind": "session", "session_id": fork.session.id}
            assert {key: value for key, value in composition_ref.items() if key != "owner"} == {
                key: value for key, value in source_ref.items() if key != "owner"
            }
            forked_composition = FrozenComposition.from_payload(stored_fork.session.metadata["execution_composition"])
            source_composition = FrozenComposition.from_payload(source.session.metadata["execution_composition"])
            assert new_store.load_execution_composition(ref=CompositionRef.model_validate(composition_ref)) == forked_composition
            assert forked_composition == source_composition
            fork_replay = new_runtime.resume(fork.session.id)
            assert fork_replay.session.session.id == fork.session.id
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_bundle_collision_rebinds_active_native_batch_before_approval_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    source_workspace = tmp_path / "source-workspace"
    target_workspace = tmp_path / "target-workspace"
    source_workspace.mkdir()
    target_workspace.mkdir()
    initial = "before\n"
    (source_workspace / "sample.txt").write_text(initial, encoding="utf-8")
    (target_workspace / "sample.txt").write_text(initial, encoding="utf-8")
    session_id = "bundle-pending-approval"
    read_id = "read_bundle_call"
    write_id = "write_bundle_call"
    read_call = {
        "index": 0,
        "id": read_id,
        "type": "function",
        "function": {"name": "read", "arguments": '{"path":"sample.txt"}'},
    }
    write_arguments = {
        "path": "sample.txt",
        "content": "after\n",
        "expectedHash": hashlib.sha256(initial.encode()).hexdigest(),
    }
    write_call = {
        "index": 1,
        "id": write_id,
        "type": "function",
        "function": {"name": "write", "arguments": json.dumps(write_arguments)},
    }
    server = OpenAILoopbackServer(
        [
            openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "Read the file before requesting the approved replacement.",
                    "tool_calls": [read_call, write_call],
                },
                finish="tool_calls",
            ),
            openai_response(message={"role": "assistant", "content": "Collision session complete."}, finish="stop"),
            openai_response(message={"role": "assistant", "content": "Imported write complete."}, finish="stop"),
        ]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def make_runtime(workspace: Path, database_path: Path) -> tuple[VoidCodeRuntime, SqliteSessionStore]:
        store = SqliteSessionStore(database_path=database_path)
        runtime = VoidCodeRuntime(
            workspace=workspace,
            repositories=RuntimeRepositories(store, store, store, store, store, store, store),
            tool_registry=ToolRegistry.from_tools([ReadTool(), WriteTool()]),
            config=RuntimeConfig(
                approval_mode="ask",
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
        return runtime, store

    source_runtime, source_store = make_runtime(source_workspace, tmp_path / "source.sqlite3")
    target_runtime, target_store = make_runtime(target_workspace, tmp_path / "target.sqlite3")
    try:
        with source_runtime:
            waiting = source_runtime.run(RuntimeRequest(prompt="Read and replace sample.txt", session_id=session_id))
            approval = next(event for event in waiting.events if event.event_type == "runtime.approval_requested")
            assert waiting.session.status == "waiting"
            bundle_path = tmp_path / "pending-session.vcsession.zip"
            write_session_bundle(source_runtime.export_session_bundle(session_id=session_id), path=bundle_path)
        source_events_before = source_store.load_session(workspace=source_workspace, session_id=session_id).events

        with target_runtime:
            collision = target_runtime.run(RuntimeRequest(prompt="Occupy the original session ID", session_id=session_id))
            assert collision.session.status == "completed"
            imported = target_runtime.import_session_bundle_file(bundle_path=bundle_path)
            assert len(imported.imported_session_ids) == 1
            imported_session_id = imported.imported_session_ids[0]
            assert imported_session_id != session_id
            resumed = target_runtime.resume(
                imported_session_id,
                approval_request_id=str(approval.payload["request_id"]),
                approval_decision="allow",
            )

        assert resumed.session.status == "completed"
        assert (target_workspace / "sample.txt").read_text(encoding="utf-8") == "after\n"
        assert (source_workspace / "sample.txt").read_text(encoding="utf-8") == initial
        assert source_store.load_session(workspace=source_workspace, session_id=session_id).events == source_events_before
        assert target_store.load_session(workspace=target_workspace, session_id=session_id).session.status == "completed"
        started = [
            event.payload["tool_call_id"]
            for event in resumed.events
            if event.event_type == "runtime.tool_started" and event.payload.get("tool_call_id") in {read_id, write_id}
        ]
        completed = [
            event.payload["tool_call_id"]
            for event in resumed.events
            if event.event_type == "runtime.tool_completed" and event.payload.get("tool_call_id") in {read_id, write_id}
        ]
        assert started == completed == [read_id, write_id]
        messages = server.requests[-1]["messages"]
        assert isinstance(messages, list)
        assistant_messages = [
            (index, message)
            for index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("tool_calls"), list)
        ]
        assert len(assistant_messages) == 1
        assistant_index, assistant_message = assistant_messages[0]
        calls = assistant_message["tool_calls"]
        assert isinstance(calls, list)
        assert [(call["id"], call["function"]["name"], json.loads(call["function"]["arguments"])) for call in calls if isinstance(call, dict)] == [
            (read_id, "read", {"path": "sample.txt"}),
            (write_id, "write", write_arguments),
        ]
        results = [(index, message) for index, message in enumerate(messages) if isinstance(message, dict) and message.get("role") == "tool"]
        assert [message.get("tool_call_id") for _, message in results] == [read_id, write_id]
        user_index = next(
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "user" and "Read and replace sample.txt" in str(message.get("content"))
        )
        assert user_index < assistant_index < results[0][0] < results[1][0]
        assert assistant_message.get("reasoning_content") == "Read the file before requesting the approved replacement."
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_openai_sdk_rejects_duplicate_native_write_calls_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    source = tmp_path / "sample.txt"
    source.write_text("before\n", encoding="utf-8")
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    write_arguments = json.dumps({"path": "sample.txt", "content": "after\n", "expectedHash": expected_hash})
    tool_calls = [
        {
            "index": index,
            "id": "call_duplicate_write",
            "type": "function",
            "function": {"name": "write", "arguments": write_arguments},
        }
        for index in (0, 1)
    ]
    server = OpenAILoopbackServer(
        [
            openai_response(
                message={"role": "assistant", "content": None, "tool_calls": tool_calls},
                finish="tool_calls",
            )
        ]
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = SqliteSessionStore(database_path=tmp_path / "duplicate-write.sqlite3")
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        repositories=RuntimeRepositories(store, store, store, store, store, store, store),
        tool_registry=ToolRegistry.from_tools([WriteTool()]),
        config=RuntimeConfig(
            approval_mode="yolo",
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
        permission_policy=PermissionPolicy(mode="yolo"),
    )
    try:
        with runtime:
            response = runtime.run(RuntimeRequest(prompt="write sample.txt", session_id="duplicate-native-write"))

        event_types = [event.event_type for event in response.events]
        assert response.session.status == "failed"
        assert "runtime.failed" in event_types
        assert not any(event_type in {"runtime.tool_started", "runtime.tool_completed"} for event_type in event_types)
        assert source.read_text(encoding="utf-8") == "before\n"
        assert len(server.requests) == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
