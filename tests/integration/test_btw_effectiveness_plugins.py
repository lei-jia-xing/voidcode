"""End-to-end integration tests for the btw and effectiveness plugins on the real stack."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel
from voidcode.sdk.manifest import PluginFileIdentity, PluginManifest, PluginToolDeclaration
from voidcode.sdk.tools import ToolEffect

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
        path = str(call.arguments.get("path", "note.txt"))
        if path == "boom.txt":
            return ToolFailure(
                tool_name=self.definition.name,
                error="notes_write refused boom.txt",
                output=TextOutput(text="refused"),
            )
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text="notes written"))


def create_plugin(config):
    return Plugin(tools=(NotesWriteTool(),))
'''
_NOTES_PLUGIN_SOURCE = _NOTES_PLUGIN_SOURCE.replace(
    "from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess",
    "from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess",
)
_CREATED_KERNELS: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_created_kernels() -> Iterator[None]:
    yield
    while _CREATED_KERNELS:
        _CREATED_KERNELS.pop().close()


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _notes_plugin(workspace: Path) -> Path:
    root = workspace / ".test-plugins" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "notes_plugin.py"
    source.write_text(_NOTES_PLUGIN_SOURCE, encoding="utf-8")
    manifest = PluginManifest(
        plugin_id="notes",
        version="1.0.0",
        entrypoint="notes_plugin:create_plugin",
        files=(PluginFileIdentity(path="notes_plugin.py", sha256=hashlib.sha256(source.read_bytes()).hexdigest()),),
        tools=(
            PluginToolDeclaration(
                name="notes_write",
                description="Write a note (test tool).",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "additionalProperties": False,
                },
                effects=(ToolEffect.WRITE,),
                replay_policy="never",
            ),
        ),
    )
    (root / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")
    return root


def _kernel(tmp_path: Path, server: OpenAILoopbackServer, *plugin_ids: str) -> Kernel:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {
        # The bundled files plugin is not under test here and is rewritten by another P4 lane.
        "files": {"enabled": False},
        "notes": {"path": str(_notes_plugin(workspace))},
    }
    for plugin_id in plugin_ids:
        plugins[plugin_id] = {"path": str(_PLUGIN_ROOT / plugin_id)}
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "yolo"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": plugins,
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=state_dir, trusted_plugins=frozenset({*plugin_ids, "notes"}))
    _CREATED_KERNELS.append(kernel)
    return kernel


def _invoke(kernel: Kernel, session_id: str, name: str, arguments: dict[str, object]):
    return kernel.invoke_command(session_id, name, arguments, expected_revision=kernel.command_catalog().revision_id)


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
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, "btw")
        session_id = kernel.create_session().session_id
        assert kernel.run("main question", session_id=session_id).status == "completed"

        result = _invoke(kernel, session_id, "btw", {"question": "what is 2+2?"})
        assert result.status == "ok"
        assert result.output == "side answer"

        # The side question is one governed model query: no tools, no session history.
        assert len(server.requests) == 2
        btw_messages = _messages(server.requests[1])
        assert [message["role"] for message in btw_messages] == ["user"]
        prompt = str(btw_messages[0]["content"])
        assert "what is 2+2?" in prompt
        assert "NO tools" in prompt
        assert "main question" not in prompt
        assert server.requests[1].get("tools") in (None, [])

        # The kernel records the canonical diagnostic fact; the plugin stages nothing.
        event_types = [event.event_type for event in kernel.events_after(session_id)]
        assert "kernel.model_query" in event_types
        assert not [event for event in event_types if event.startswith("plugin.btw.")]

        empty = _invoke(kernel, session_id, "btw", {"question": "   "})
        assert empty.status == "error"
        assert empty.error == "side question must not be empty"


def test_effectiveness_projects_real_session_events(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-ok",
                        "type": "function",
                        "function": {"name": "notes_write", "arguments": json.dumps({"path": "ok.txt"})},
                    }
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-bad",
                        "type": "function",
                        "function": {"name": "notes_write", "arguments": json.dumps({"path": "boom.txt"})},
                    }
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
        ),
        openai_response(
            message={"role": "assistant", "content": "done"},
            finish="stop",
            usage={"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, "effectiveness")
        session_id = kernel.create_session().session_id
        assert kernel.run("write two notes", session_id=session_id).status == "completed"

        result = _invoke(kernel, session_id, "effectiveness", {})
        assert result.status == "ok"
        assert result.data is not None
        rows = {row["tool"]: row for row in result.data["tools"]}
        assert rows["notes_write"]["calls"] == 2
        assert rows["notes_write"]["successes"] == 1
        assert rows["notes_write"]["errors"] == 1
        usage = result.data["provider_usage"]
        assert usage["input_tokens"] == 90
        assert usage["output_tokens"] == 21
        assert result.data["privacy"]["stores_source_content"] is False
        assert "notes_write" in str(result.output)

        empty = _invoke(kernel, kernel.create_session().session_id, "effectiveness", {})
        assert empty.status == "ok"
        assert empty.data is not None
        assert tuple(empty.data["tools"]) == ()
