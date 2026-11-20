"""End-to-end integration tests for the Plan plugin's restrictive overlay.

Proves on the real core stack that entering plan mode hides and denies write-tier tools,
contributes the verbatim plan instruction, survives a restart, and that leaving plan mode
restores writes while a user-frozen read-only policy stays in force.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.core_factory import loopback_server
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.plugins.plan.plan_plugin import PLAN_MODE_INSTRUCTION

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
_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _write_tool_plugin(workspace: Path) -> Path:
    root = workspace / ".test-plugins" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(_WRITE_TOOL_PLUGIN_SOURCE, encoding="utf-8")
    return root


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    read_only: bool = False,
    state_dir: Path | None = None,
) -> Core:
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
    core = open_core(workspace, state_dir=resolved_state)
    _CREATED_CORES.append(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


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
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id

        # Baseline: the write-tier tool is advertised and the plan instruction is absent.
        assert core.run("hi", session_id=session_id).status == "completed"
        assert "notes_write" in _tool_names(server.requests[0])
        assert PLAN_MODE_INSTRUCTION not in _text(server.requests[0])

        # Enter plan mode: the overlay hides the write tool and contributes the instruction.
        entered = _invoke(core, session_id, "plan", {})
        assert entered.status == "ok"
        assert entered.data == {"active": True, "changed": True}
        assert _invoke(core, session_id, "plan.status", {}).output == "Mode: plan"

        assert core.run("plan it", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[1])
        assert PLAN_MODE_INSTRUCTION in _text(server.requests[1])

        # The overlay survives a restart: still hidden, still instructed.
        core.close()
        _CREATED_CORES.remove(core)
        reopened = _core(tmp_path, server, state_dir=tmp_path / "state")
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
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, read_only=True)
        session_id = core.create_session().session_id
        assert _invoke(core, session_id, "plan", {}).status == "ok"
        assert _invoke(core, session_id, "plan.off", {}).status == "ok"
        assert core.run("still frozen", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[0]), "the frozen policy outlives the plan overlay"
