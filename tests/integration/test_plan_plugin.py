"""End-to-end integration tests for the Plan plugin's restrictive overlay.

Proves on the real kernel stack that entering plan mode hides and denies write-tier tools,
contributes the verbatim plan instruction, survives a restart, and that leaving plan mode
restores writes while a user-frozen read-only policy stays in force.
"""

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
from voidcode.plugins.plan.plan_plugin import PLAN_MODE_INSTRUCTION
from voidcode.sdk.manifest import PluginFileIdentity, PluginManifest, PluginToolDeclaration
from voidcode.sdk.tools import ToolEffect

_PLAN_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "plan"
_WRITE_TOOL_PLUGIN_SOURCE = '''"""Test-only plugin exposing one write-tier tool."""

from __future__ import annotations

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class NotesWriteTool:
    definition = ToolDefinition(
        name="notes_write",
        description="Write a note (test write-tier tool).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text="notes written"))


def create_plugin(config):
    return Plugin(tools=(NotesWriteTool(),))
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


def _write_tool_plugin(workspace: Path) -> Path:
    root = workspace / ".test-plugins" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "notes_plugin.py"
    source.write_text(_WRITE_TOOL_PLUGIN_SOURCE, encoding="utf-8")
    manifest = PluginManifest(
        plugin_id="notes",
        version="1.0.0",
        entrypoint="notes_plugin:create_plugin",
        files=(PluginFileIdentity(path="notes_plugin.py", sha256=hashlib.sha256(source.read_bytes()).hexdigest()),),
        tools=(
            PluginToolDeclaration(
                name="notes_write",
                description="Write a note (test write-tier tool).",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effects=(ToolEffect.WRITE,),
                replay_policy="never",
            ),
        ),
    )
    (root / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")
    return root


def _kernel(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    read_only: bool = False,
    state_dir: Path | None = None,
) -> Kernel:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    resolved_state = state_dir if state_dir is not None else tmp_path / "state"
    resolved_state.mkdir(parents=True, exist_ok=True)
    notes_root = _write_tool_plugin(workspace)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "ask", "read_only": read_only},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            "files": {"enabled": False},
            "plan": {"path": str(_PLAN_PLUGIN_ROOT)},
            "notes": {"path": str(notes_root)},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=resolved_state, trusted_plugins=frozenset({"plan", "notes"}))
    _CREATED_KERNELS.append(kernel)
    return kernel


def _invoke(kernel: Kernel, session_id: str, name: str, arguments: dict[str, object]):
    return kernel.invoke_command(session_id, name, arguments, expected_revision=kernel.command_catalog().revision_id)


def _tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for entry in tools:
        if isinstance(entry, dict) and isinstance(entry.get("function"), dict):
            names.append(str(entry["function"]["name"]))  # type: ignore[index]
    return names


def _text(request: dict[str, object]) -> str:
    return json.dumps(request.get("messages"), ensure_ascii=False)


def test_plan_mode_hides_and_denies_write_tools_until_plan_off(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(
            message={"role": "assistant", "content": "planned"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
        openai_response(
            message={"role": "assistant", "content": "wrote nothing"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
        openai_response(
            message={"role": "assistant", "content": "plain turn"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server)
        session_id = kernel.create_session().session_id

        # Baseline: the write-tier tool is advertised and the plan instruction is absent.
        assert kernel.run("hi", session_id=session_id).status == "completed"
        assert "notes_write" in _tool_names(server.requests[0])
        assert PLAN_MODE_INSTRUCTION not in _text(server.requests[0])

        # Enter plan mode: the overlay hides the write tool and contributes the instruction.
        entered = _invoke(kernel, session_id, "plan", {})
        assert entered.status == "ok"
        assert entered.data == {"active": True, "changed": True}
        assert _invoke(kernel, session_id, "plan.status", {}).output == "Mode: plan"

        assert kernel.run("plan it", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[1])
        assert PLAN_MODE_INSTRUCTION in _text(server.requests[1])
        descriptor = {c.name: c for c in kernel.command_catalog().commands}["plan"]
        assert descriptor.direct_user is True

        # The overlay survives a restart: still hidden, still instructed.
        kernel.close()
        _CREATED_KERNELS.remove(kernel)
        reopened = _kernel(tmp_path, server, state_dir=tmp_path / "state")
        assert _invoke(reopened, session_id, "plan.status", {}).output == "Mode: plan"
        assert reopened.run("still planning", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[2])
        assert PLAN_MODE_INSTRUCTION in _text(server.requests[2])

        # Leaving plan mode revokes only this owner's overlay.
        left = _invoke(reopened, session_id, "plan.off", {})
        assert left.status == "ok"
        assert left.data == {"active": False, "changed": True}
        assert _invoke(reopened, session_id, "plan.status", {}).output == "Mode: default"

        replies.append(
            openai_response(
                message={"role": "assistant", "content": "back to normal"},
                finish="stop",
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            )
        )
        assert reopened.run("normal again", session_id=session_id).status == "completed"
        assert "notes_write" in _tool_names(server.requests[3])
        assert PLAN_MODE_INSTRUCTION not in _text(server.requests[3])

        offline = _invoke(reopened, session_id, "plan.off", {})
        assert offline.data == {"active": False, "changed": False}


def test_plan_off_never_lifts_a_user_frozen_read_only(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "read-only turn"},
            finish="stop",
            usage={"prompt_tokens": 15, "completion_tokens": 4, "total_tokens": 19},
        )
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, read_only=True)
        session_id = kernel.create_session().session_id
        assert _invoke(kernel, session_id, "plan", {}).status == "ok"
        assert _invoke(kernel, session_id, "plan.off", {}).status == "ok"
        assert kernel.run("still frozen", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[0]), "the frozen policy outlives the plan overlay"
