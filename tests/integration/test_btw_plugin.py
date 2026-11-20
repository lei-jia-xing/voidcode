"""End-to-end integration tests for the btw plugin on the real stack."""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.core_factory import loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core

_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins"
_NOTES_PLUGIN_SOURCE = '''"""Test-only plugin exposing one observable tool."""

from __future__ import annotations

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class NotesWriteTool:
    definition = ToolDefinition(
        name="notes_write",
        description="Write a note (test tool).",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text="notes written"))


def create_plugin(config):
    return Plugin(tools=(NotesWriteTool(),))
'''


def _notes_plugin(workspace: Path) -> Path:
    root = workspace / ".test-plugins" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(_NOTES_PLUGIN_SOURCE, encoding="utf-8")
    return root


def _core(tmp_path: Path, server: OpenAILoopbackServer, *plugin_ids: str, approval_mode: str = "yolo") -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {
        "files": {"enabled": False},
        "notes": {"path": str(_notes_plugin(workspace))},
    }
    for plugin_id in plugin_ids:
        plugins[plugin_id] = {"path": str(_PLUGIN_ROOT / plugin_id)}
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": approval_mode},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": plugins,
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request.get("messages")
    return [entry for entry in messages if isinstance(entry, dict)] if isinstance(messages, list) else []


def test_btw_answers_without_polluting_history(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "first answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        ),
        openai_response(
            message={"role": "assistant", "content": "side answer"},
            finish="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        ),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw")
        session_id = core.create_session().session_id
        assert core.run("main question", session_id=session_id).status == "completed"

        conversation_before = [event for event in core.events_after(session_id) if event.event_type in {"core.user_message", "core.model_turn"}]
        result = _invoke(core, session_id, "btw", {"question": "what is 2+2?"})
        assert result.status == "ok"
        assert result.output == "side answer"

        # The side question reads the conversation but offers no tools.
        assert len(server.requests) == 2
        btw_messages = _messages(server.requests[1])
        assert [message["role"] for message in btw_messages] == ["user", "assistant", "user"]
        prompt = str(btw_messages[-1]["content"])
        assert "what is 2+2?" in prompt
        assert btw_messages[0]["content"] == "main question"
        assert btw_messages[1]["content"] == "first answer"
        assert server.requests[1].get("tools") in (None, [])

        # The core records the canonical diagnostic fact; the plugin stages nothing.
        event_types = [event.event_type for event in core.events_after(session_id)]
        assert "core.model_query" in event_types
        assert [
            event for event in core.events_after(session_id) if event.event_type in {"core.user_message", "core.model_turn"}
        ] == conversation_before
        assert not [event for event in event_types if event.startswith("plugin.btw.")]

        empty = _invoke(core, session_id, "btw", {"question": "   "})
        assert empty.status == "error"
        assert empty.error == "side question must not be empty"


def test_btw_uses_complete_context_while_tool_approval_is_pending(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "pending", "type": "function", "function": {"name": "notes_write", "arguments": "{}"}}],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "side answer"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, "btw", approval_mode="ask")
        session_id = core.create_session().session_id
        main = core.run("write a note", session_id=session_id)
        assert main.pending_approval is not None
        before = core.session(session_id)
        result = _invoke(core, session_id, "btw", {"question": "what did I request?"})
        assert result.status == "ok"
        messages = _messages(server.requests[1])
        assert [message["role"] for message in messages] == ["user", "user"]
        assert messages[0]["content"] == "write a note"
        assert not any(message.get("tool_calls") for message in messages)
        after = core.session(session_id)
        assert after.status == before.status
        assert after.run_id == before.run_id
