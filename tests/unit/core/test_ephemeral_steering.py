from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.core_factory import loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core, _Cursor, _Execution
from voidcode.sdk.provider import ProviderMessage
from voidcode.sdk.transcript import TranscriptTool

PLUGIN_ID = "steering-test"

PLUGIN_CODE = """from __future__ import annotations

from voidcode.sdk import Plugin, TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess


class SteeringSuccessTool:
    definition = ToolDefinition(
        name="steering_success_tool",
        description="Tool that succeeds with steering context",
        input_schema={"type": "object"},
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, call, *, context, services):
        return ToolSuccess(
            tool_name="steering_success_tool",
            output=TextOutput("success result text"),
            steering_context="Next turn must check foo.py",
        )


class SteeringFailureTool:
    definition = ToolDefinition(
        name="steering_failure_tool",
        description="Tool that fails with steering context",
        input_schema={"type": "object"},
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, call, *, context, services):
        return ToolFailure(
            tool_name="steering_failure_tool",
            error="file not found",
            output=TextOutput("error output"),
            steering_context="Retry with valid path",
        )


def create_plugin(config):
    return Plugin(
        tools=(SteeringSuccessTool(), SteeringFailureTool()),
    )
"""


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(PLUGIN_CODE, encoding="utf-8")
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo"},
        "providers": {"endpoint": {"api_key": "test-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    register_core(core)
    return core


def _find_system_guidance(messages: list[Mapping[str, object]]) -> list[str]:
    return [
        str(message["content"])
        for message in messages
        if message.get("role") == "system" and str(message.get("content", "")).startswith("[Tool Guidance]")
    ]


def test_tool_success_steering_context_appears_next_dispatch_only(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    replies = [
        _turn(_call("call-s1", "steering_success_tool")),
        _turn(output="acknowledged"),
        _turn(output="done next run"),
    ]
    with loopback_server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        session_id = "sess-steering-success"
        core.create_session(session_id)

        # First run: model calls tool, tool succeeds with steering_context
        result1 = core.run("execute success tool", session_id=session_id)
        assert result1.status == "completed"
        assert len(server.requests) == 2

        # Initial prompt dispatch has no tool guidance
        first_messages = server.requests[0]["messages"]
        assert isinstance(first_messages, list)
        assert _find_system_guidance(first_messages) == []

        # Dispatch immediately following the tool result carries the ephemeral steering context
        second_messages = server.requests[1]["messages"]
        assert isinstance(second_messages, list)
        guidance = _find_system_guidance(second_messages)
        assert guidance == ["[Tool Guidance]\nNext turn must check foo.py"]

        # Second run in the same session: steering context must NOT persist into subsequent turns
        result2 = core.run("continue after success", session_id=session_id)
        assert result2.status == "completed"
        assert len(server.requests) == 3

        third_messages = server.requests[2]["messages"]
        assert isinstance(third_messages, list)
        assert _find_system_guidance(third_messages) == []

        # Permanent transcript history must NOT contain the ephemeral guidance
        page = core.transcript_page(session_id)
        for entry in page.entries:
            if isinstance(entry.content, ProviderMessage):
                assert "[Tool Guidance]" not in (entry.content.content or "")
        tools = [entry.content for entry in page.entries if isinstance(entry.content, TranscriptTool)]
        assert len(tools) == 1
        assert tools[0].call.tool_name == "steering_success_tool"
        assert tools[0].result.steering_context == "Next turn must check foo.py"

        # Rebuilding messages from SQLite events must not include the ephemeral steering context
        session = core.store.session(session_id)
        replayed_execution = _Execution(session, _Cursor(), list(core.store.events(session_id)), "test-replay")
        replayed_messages = core._messages(replayed_execution)
        assert not any(msg.role == "system" and "[Tool Guidance]" in (msg.content or "") for msg in replayed_messages)


def test_tool_failure_steering_context_appears_next_dispatch_only(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    replies = [
        _turn(_call("call-f1", "steering_failure_tool")),
        _turn(output="handled failure"),
        _turn(output="done subsequent"),
    ]
    with loopback_server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        session_id = "sess-steering-failure"
        core.create_session(session_id)

        # First run: model calls tool, tool fails with steering_context
        result1 = core.run("execute failure tool", session_id=session_id)
        assert result1.status == "completed"
        assert len(server.requests) == 2

        # Dispatch immediately following the tool failure carries the ephemeral steering context
        second_messages = server.requests[1]["messages"]
        assert isinstance(second_messages, list)
        guidance = _find_system_guidance(second_messages)
        assert guidance == ["[Tool Guidance]\nRetry with valid path"]

        # Subsequent dispatch: does not persist
        result2 = core.run("continue after failure", session_id=session_id)
        assert result2.status == "completed"
        assert len(server.requests) == 3

        third_messages = server.requests[2]["messages"]
        assert isinstance(third_messages, list)
        assert _find_system_guidance(third_messages) == []

        # Transcript history check
        page = core.transcript_page(session_id)
        for entry in page.entries:
            if isinstance(entry.content, ProviderMessage):
                assert "[Tool Guidance]" not in (entry.content.content or "")

        # Replay from SQLite events
        session = core.store.session(session_id)
        replayed_execution = _Execution(session, _Cursor(), list(core.store.events(session_id)), "test-replay-fail")
        replayed_messages = core._messages(replayed_execution)
        assert not any(msg.role == "system" and "[Tool Guidance]" in (msg.content or "") for msg in replayed_messages)


def test_ephemeral_steering_multiple_tools_and_cleared_after_messages(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir(parents=True, exist_ok=True)
    with loopback_server([]) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        session_id = "sess-steering-unit"
        core.create_session(session_id)
        session = core.store.session(session_id)

        execution = _Execution(session, _Cursor(), list(core.store.events(session_id)), "unit-run")
        execution.ephemeral_steering.extend(["Guidance item 1", "Guidance item 2"])

        messages = core._messages(execution)
        guidance_messages = [msg for msg in messages if msg.role == "system" and (msg.content or "").startswith("[Tool Guidance]")]
        assert len(guidance_messages) == 1
        assert guidance_messages[0].content == "[Tool Guidance]\nGuidance item 1\nGuidance item 2"

        # Ephemeral steering is cleared after assembly
        assert execution.ephemeral_steering == []

        # Subsequent call does not include it
        messages_next = core._messages(execution)
        assert not any(msg.role == "system" and "[Tool Guidance]" in (msg.content or "") for msg in messages_next)
