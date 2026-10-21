"""End-to-end integration test for the Skills plugin on the real core stack.

Proves the discovery -> catalog context -> skill tool path with a real provider request:
the catalog is contributed to the model, the model loads a body by name, and disabling the
plugin removes the tool, the context and the command.
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


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    skill_plugin_enabled: bool,
    skills_enabled: bool,
) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "ask"},
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
                "enabled": skill_plugin_enabled,
                "config": {"enabled": skills_enabled},
            },
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _write_skill(workspace: Path, *, name: str, description: str, body: str) -> Path:
    root = workspace / ".voidcode" / "skills" / name
    root.mkdir(parents=True, exist_ok=True)
    entry = root / "SKILL.md"
    entry.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return entry


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request.get("messages")
    return [entry for entry in messages if isinstance(entry, dict)] if isinstance(messages, list) else []


def _tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for entry in tools:
        if isinstance(entry, dict) and isinstance(entry.get("function"), dict):
            names.append(str(entry["function"]["name"]))  # type: ignore[index]
    return names


def test_skill_catalog_context_and_on_demand_body_on_the_real_stack(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-skill-1",
                        "type": "function",
                        "function": {"name": "skill", "arguments": json.dumps({"name": "release"})},
                    }
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
        ),
        openai_response(
            message={"role": "assistant", "content": "Followed the release skill."},
            finish="stop",
            usage={"prompt_tokens": 40, "completion_tokens": 8, "total_tokens": 48},
        ),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, skill_plugin_enabled=True, skills_enabled=True)
        entry = _write_skill(
            tmp_path / "workspace",
            name="release",
            description="Cut a release",
            body="# Steps\n\n1. bump the version",
        )
        session_id = core.create_session().session_id

        result = core.run("cut a release", session_id=session_id)
        assert result.status == "completed"
        assert result.output == "Followed the release skill."

        first_request = _messages(server.requests[0])
        assert "skill" in _tool_names(server.requests[0])
        catalog = [message for message in first_request if message.get("role") == "system"]
        catalog_text = "\n".join(str(message.get("content")) for message in catalog)
        assert "<available_skills>" in catalog_text
        assert "<name>release</name>" in catalog_text
        assert "<description>Cut a release</description>" in catalog_text
        assert entry.resolve().as_uri() in catalog_text

        tool_messages = [message for message in _messages(server.requests[1]) if message.get("role") == "tool"]
        assert len(tool_messages) == 1
        body = str(tool_messages[0]["content"])
        assert f"## Skill: release\n**Description**: Cut a release\nsourcePath: {entry}\nbaseDirectory: {entry.parent}" in body
        assert "# Steps\n\n1. bump the version" in body

        listed = core.invoke_command(session_id, "skills", {}, expected_revision=core.command_catalog().revision_id)
        assert listed.status == "ok"
        assert listed.output is not None
        assert "[enabled] release — Cut a release" in listed.output


def test_disabled_skills_plugin_contributes_nothing(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "plain answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        )
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, skill_plugin_enabled=False, skills_enabled=True)
        _write_skill(tmp_path / "workspace", name="release", description="Cut a release", body="body")
        session_id = core.create_session().session_id
        assert core.run("hello", session_id=session_id).status == "completed"
        assert "skill" not in _tool_names(server.requests[0])
        request_text = json.dumps(_messages(server.requests[0]), ensure_ascii=False)
        assert "<available_skills>" not in request_text
        names = [descriptor.name for descriptor in core.command_catalog().commands]
        assert "skills" not in names


def test_plugin_enabled_but_skills_disabled_in_config(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "plain answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        )
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, skill_plugin_enabled=True, skills_enabled=False)
        _write_skill(tmp_path / "workspace", name="release", description="Cut a release", body="body")
        session_id = core.create_session().session_id
        assert core.run("hello", session_id=session_id).status == "completed"
        # The tool stays advertised (discovery is per call), but no catalog context is contributed.
        assert "skill" in _tool_names(server.requests[0])
        request_text = json.dumps(_messages(server.requests[0]), ensure_ascii=False)
        assert "<available_skills>" not in request_text
        listed = core.invoke_command(session_id, "skills", {}, expected_revision=core.command_catalog().revision_id)
        assert listed.output == "Skills are disabled."
