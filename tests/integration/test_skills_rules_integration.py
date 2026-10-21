"""Integration test for the skills plugin's rule context on the real core stack.

Proves the workspace ``AGENTS.md`` hierarchy and the ``.voidcode/rules`` rulebook reach the
provider prompt per dispatch, that a tool touching a nested path adds that directory's rule
file to the NEXT dispatch, and that the touched path is durable in the plugin's state row.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

_SKILLS_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "skills"
_TOUCH_PLUGIN_ID = "rule-touch"
_TOUCH_TOOL = "touch_file"

_TOUCH_PLUGIN_IMPL = '''
"""Test-only plugin: one tool that writes the path it is given."""

from pathlib import Path

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class TouchFile:
    definition = ToolDefinition(
        name="touch_file",
        description="Write the given workspace-relative path.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path",),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        target = Path(str(call.arguments.get("path", "")))
        if not target.is_absolute():
            target = context.require_workspace() / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("touched", encoding="utf-8")
        return ToolSuccess(call.tool_name, TextOutput(f"touched {target.name}"))


def create_plugin(config):
    return Plugin(tools=(TouchFile(),))
'''


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


_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _write_touch_plugin(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(_TOUCH_PLUGIN_IMPL, encoding="utf-8")
    return root


def _core(tmp_path: Path, server: OpenAILoopbackServer, *, skills_enabled: bool) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
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
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            "skills": {
                "path": str(_SKILLS_PLUGIN_ROOT),
                "enabled": True,
                "config": {"enabled": skills_enabled},
            },
            _TOUCH_PLUGIN_ID: {"path": str(_write_touch_plugin(tmp_path / "touch-plugin")), "enabled": True},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _prompt_text(request: dict[str, object]) -> str:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return ""
    return "\n".join(str(message.get("content", "")) for message in messages if isinstance(message, dict))


def _write_workspace_rules(workspace: Path) -> None:
    (workspace / "AGENTS.md").write_text("root-rule-marker\n", encoding="utf-8")
    (workspace / "sub").mkdir(parents=True, exist_ok=True)
    (workspace / "sub" / "AGENTS.md").write_text("sub-rule-marker\n", encoding="utf-8")
    rules = workspace / ".voidcode" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "style.md").write_text(
        "---\nname: style\napplication: always_apply\ndescription: House style\n---\n\nrulebook-marker\n",
        encoding="utf-8",
    )


def test_first_dispatch_carries_the_agents_hierarchy_and_rulebook(tmp_path: Path) -> None:
    with _loopback_server([_turn(output="plain answer")]) as server:
        core = _core(tmp_path, server, skills_enabled=False)
        workspace = tmp_path / "workspace"
        _write_workspace_rules(workspace)
        session_id = core.create_session().session_id

        assert core.run("hello", session_id=session_id).status == "completed"

        prompt = _prompt_text(server.requests[0])
        assert "Runtime file rules are active for touched workspace paths." in prompt
        assert "Rule file: AGENTS.md\nroot-rule-marker" in prompt
        assert "Runtime rulebook always-apply rules are workspace guidance only" in prompt
        assert "Rule style (scope=workspace, precedence=100):\nrulebook-marker" in prompt
        # Nothing was touched yet: the nested rule file stays out of the prompt.
        assert "sub-rule-marker" not in prompt
        # The skills catalog stays gated on the plugin's own skills config.
        assert "<available_skills>" not in prompt


def test_touched_path_brings_its_rule_file_into_the_next_dispatch(tmp_path: Path) -> None:
    replies = [
        _turn(_call("call-touch-1", _TOUCH_TOOL, path="sub/file.txt")),
        _turn(output="touched the file"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, skills_enabled=False)
        workspace = tmp_path / "workspace"
        _write_workspace_rules(workspace)
        session_id = core.create_session().session_id

        result = core.run("touch sub/file.txt", session_id=session_id)
        assert result.status == "completed"
        assert result.output == "touched the file"
        assert len(server.requests) == 2

        first = _prompt_text(server.requests[0])
        assert "sub-rule-marker" not in first
        second = _prompt_text(server.requests[1])
        assert "Rule file: sub/AGENTS.md\nsub-rule-marker" in second
        assert "Rule file: AGENTS.md\nroot-rule-marker" in second

        stored, version = core.store.plugin_state("skills", "session", session_id)
        assert stored is not None
        assert version == 1
        assert stored["schema_version"] == 1
        assert list(stored["touched_paths"]) == ["sub/file.txt"]  # type: ignore[arg-type]
        assert stored["cursor_sequence"] >= 1


def test_skill_catalog_precedes_the_rule_context_when_skills_are_enabled(tmp_path: Path) -> None:
    with _loopback_server([_turn(output="plain answer")]) as server:
        core = _core(tmp_path, server, skills_enabled=True)
        workspace = tmp_path / "workspace"
        _write_workspace_rules(workspace)
        skill = workspace / ".voidcode" / "skills" / "release"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("---\nname: release\ndescription: Cut a release\n---\n\nsteps\n", encoding="utf-8")
        session_id = core.create_session().session_id

        assert core.run("hello", session_id=session_id).status == "completed"

        prompt = _prompt_text(server.requests[0])
        assert "<name>release</name>" in prompt
        assert prompt.index("<available_skills>") < prompt.index("Runtime file rules are active for touched workspace paths.")
