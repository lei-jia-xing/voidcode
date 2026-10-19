"""End-to-end integration test for the formatter plugin's in-invocation hook."""

from __future__ import annotations

import hashlib
import json
import sys
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

_FORMATTER_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "formatter"
_NOTES_SOURCE = '''"""Test-only note writer."""

from __future__ import annotations

from pathlib import Path

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class WriteTool:
    definition = ToolDefinition(
        name="write",
        description="Write a small note file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        workspace = context.require_workspace()
        target = workspace / str(call.arguments["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(call.arguments["content"]), encoding="utf-8")
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text="wrote note"))


def create_plugin(config):
    return Plugin(tools=(WriteTool(),))
'''
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


def _notes_root(workspace: Path) -> Path:
    root = workspace / ".test-plugins" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "notes_plugin.py"
    source.write_text(_NOTES_SOURCE, encoding="utf-8")
    manifest = PluginManifest(
        plugin_id="notes",
        version="1.0.0",
        entrypoint="notes_plugin:create_plugin",
        files=(PluginFileIdentity(path="notes_plugin.py", sha256=hashlib.sha256(source.read_bytes()).hexdigest()),),
        tools=(
            PluginToolDeclaration(
                name="write",
                description="Write a small note file.",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                effects=(ToolEffect.WRITE,),
                replay_policy="never",
            ),
        ),
    )
    (root / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")
    return root


def _kernel(tmp_path: Path, server: OpenAILoopbackServer, *, format_on_write: bool) -> Kernel:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "yolo"},
        "providers": {"endpoint": {"api_key": "loopback-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {
            "files": {"enabled": False},
            "notes": {"path": str(_notes_root(workspace))},
            "formatter": {
                "path": str(_FORMATTER_ROOT),
                "config": {
                    "format_on_write": format_on_write,
                    "languages": {
                        ".txt": {
                            "command": [sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write('[formatted]')"],
                            "cwd": "workspace",
                        }
                    },
                },
            },
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=state_dir, trusted_plugins=frozenset({"notes", "formatter"}))
    _CREATED_KERNELS.append(kernel)
    return kernel


def _write_replies() -> list[dict[str, object]]:
    return [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-note-1",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps({"path": "note.txt", "content": "hello"}),
                        },
                    }
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
        openai_response(
            message={"role": "assistant", "content": "note written"},
            finish="stop",
            usage={"prompt_tokens": 25, "completion_tokens": 5, "total_tokens": 30},
        ),
    ]


def test_enabled_formatter_runs_inside_the_write_invocation(tmp_path: Path) -> None:
    with _loopback_server(_write_replies()) as server:
        kernel = _kernel(tmp_path, server, format_on_write=True)
        session_id = kernel.create_session().session_id
        result = kernel.run("write a note", session_id=session_id)
        assert result.status == "completed"

        # The formatter ran inside the same governed write invocation: the file it wrote changed.
        assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "hello[formatted]"

        # The durable tool result carries exactly one completed tool call, unmodified by the hook.
        events = [event for event in kernel.events_after(session_id) if event.event_type == "kernel.tool_completed"]
        assert len(events) == 1
        assert events[0].payload["result"]["status"] == "ok"


def test_disabled_formatter_leaves_the_file_untouched(tmp_path: Path) -> None:
    with _loopback_server(_write_replies()) as server:
        kernel = _kernel(tmp_path, server, format_on_write=False)
        session_id = kernel.create_session().session_id
        assert kernel.run("write a note", session_id=session_id).status == "completed"
        assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "hello"
