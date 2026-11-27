"""End-to-end integration test for the Skills plugin on the real core stack.

Proves the discovery -> catalog context -> skill:// reader path with a real provider
request: the catalog is contributed to the model, the model loads a body by URI, and
disabling the plugin removes the reader, the context and the command.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.core_factory import loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.tools import ToolCall, ToolFailure, ToolSuccess

_SKILLS_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "skills"


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer | None,
    *,
    skill_plugin_enabled: bool,
    skills_enabled: bool,
) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    providers: dict[str, object] = {}
    model: dict[str, object] = {}
    if server is not None:
        providers = {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        }
        model = {"model": "endpoint/test-model"}
    config = {
        "schema_version": 1,
        "model": model,
        "policy": {"approval_mode": "ask"},
        "providers": providers,
        "plugins": {
            "skills": {
                "path": str(_SKILLS_PLUGIN_ROOT),
                "enabled": skill_plugin_enabled,
                "config": {"enabled": skills_enabled},
            },
        },
    }
    (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
    return core


def _write_skill(workspace: Path, *, name: str, description: str, body: str, folder: str | None = None) -> Path:
    root = workspace / ".voidcode" / "skills" / (folder or name)
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
                        "id": "call-read-1",
                        "type": "function",
                        "function": {"name": "read", "arguments": json.dumps({"path": "skill://release"})},
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
    with loopback_server(replies) as server:
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
        assert "read" in _tool_names(server.requests[0])
        assert "skill" not in _tool_names(server.requests[0])
        catalog = [message for message in first_request if message.get("role") == "system"]
        catalog_text = "\n".join(str(message.get("content")) for message in catalog)
        assert "<available_skills>" in catalog_text
        assert "<name>release</name>" in catalog_text
        assert "<description>Cut a release</description>" in catalog_text
        assert "<location>skill://release</location>" in catalog_text
        assert entry.resolve().as_uri() in catalog_text
        assert 'read(path="skill://<name>")' in catalog_text

        tool_messages = [message for message in _messages(server.requests[1]) if message.get("role") == "tool"]
        assert len(tool_messages) == 1
        body = str(json.loads(str(tool_messages[0]["content"]))["text"])
        assert f"## Skill: release\n**Description**: Cut a release\nsourcePath: {entry}\nbaseDirectory: {entry.parent}" in body
        assert "# Steps\n\n1. bump the version" in body

        listed = core.invoke_command(session_id, "skills", {}, expected_revision=core.command_catalog().revision_id)
        assert listed.status == "ok"
        assert listed.output is not None
        assert "[enabled] release — Cut a release" in listed.output


def test_broken_skill_does_not_break_catalog_rules_or_reads(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "plain answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        )
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, skill_plugin_enabled=True, skills_enabled=True)
        workspace = tmp_path / "workspace"
        entry = _write_skill(workspace, name="release", description="Cut a release", body="steps")
        broken = workspace / ".voidcode" / "skills" / "broken" / "SKILL.md"
        broken.parent.mkdir(parents=True)
        broken.write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")
        (workspace / "AGENTS.md").write_text("root-rule-marker\n", encoding="utf-8")
        session_id = core.create_session().session_id

        assert core.run("hello", session_id=session_id).status == "completed"
        prompt = "\n".join(str(message.get("content", "")) for message in _messages(server.requests[0]))
        assert "<name>release</name>" in prompt
        assert "<skill_diagnostics>" in prompt
        assert "root-rule-marker" in prompt

        listed = core.invoke_command(session_id, "skills", {}, expected_revision=core.command_catalog().revision_id)
        assert listed.status == "ok"
        assert listed.output is not None
        assert "[enabled] release" in listed.output
        assert "[broken] broken" in listed.output

        context = ToolContext(
            workspace=workspace,
            session_id=session_id,
            run_id="run-broken-skill",
            invocation_id="inv-broken-skill",
            revision_id=core.plugin_runtime.revision_id,
            actor="model",
        )
        served = core.plugin_runtime.invoke(ToolCall("read", {"path": "skill://release"}), context=context)
        assert isinstance(served, ToolSuccess)
        assert f"sourcePath: {entry}" in str(getattr(served.output, "text", ""))
        failed = core.plugin_runtime.invoke(ToolCall("read", {"path": "skill://broken"}), context=context)
        assert isinstance(failed, ToolFailure)
        assert "unavailable" in failed.error and "invalid YAML" in failed.error


def test_duplicate_skill_names_do_not_break_catalog_rules_or_reads(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "plain answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        )
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, skill_plugin_enabled=True, skills_enabled=True)
        workspace = tmp_path / "workspace"
        first = _write_skill(workspace, name="release", description="Cut a release", body="first body")
        _write_skill(
            workspace,
            name="release",
            description="Duplicate",
            body="duplicate body",
            folder="release-copy",
        )
        _write_skill(workspace, name="other", description="Other skill", body="other body")
        (workspace / "AGENTS.md").write_text("root-rule-marker\n", encoding="utf-8")
        session_id = core.create_session().session_id

        assert core.run("hello", session_id=session_id).status == "completed"
        prompt = "\n".join(str(message.get("content", "")) for message in _messages(server.requests[0]))
        assert "<name>release</name>" in prompt
        assert "<name>other</name>" in prompt
        assert "<skill_diagnostics>" in prompt
        assert "release (duplicate 1)" in prompt
        assert "root-rule-marker" in prompt

        listed = core.invoke_command(session_id, "skills", {}, expected_revision=core.command_catalog().revision_id)
        assert listed.status == "ok"
        assert listed.output is not None
        assert "[enabled] release" in listed.output
        assert "[broken] release (duplicate 1)" in listed.output

        context = ToolContext(
            workspace=workspace,
            session_id=session_id,
            run_id="run-duplicate-skill",
            invocation_id="inv-duplicate-skill",
            revision_id=core.plugin_runtime.revision_id,
            actor="model",
        )
        served = core.plugin_runtime.invoke(ToolCall("read", {"path": "skill://release"}), context=context)
        assert isinstance(served, ToolSuccess)
        assert f"sourcePath: {first}" in str(getattr(served.output, "text", ""))
        unrelated = core.plugin_runtime.invoke(ToolCall("read", {"path": "skill://other"}), context=context)
        assert isinstance(unrelated, ToolSuccess)
        assert "other body" in str(getattr(unrelated.output, "text", ""))

        broken = core.plugin_runtime.invoke(ToolCall("read", {"path": "skill://release (duplicate 1)"}), context=context)
        assert isinstance(broken, ToolFailure)
        assert "duplicate skill name" in broken.error


def test_disabled_skills_plugin_contributes_nothing(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "plain answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        )
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, skill_plugin_enabled=False, skills_enabled=True)
        _write_skill(tmp_path / "workspace", name="release", description="Cut a release", body="body")
        session_id = core.create_session().session_id
        assert core.run("hello", session_id=session_id).status == "completed"
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
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, skill_plugin_enabled=True, skills_enabled=False)
        _write_skill(tmp_path / "workspace", name="release", description="Cut a release", body="body")
        session_id = core.create_session().session_id
        assert core.run("hello", session_id=session_id).status == "completed"
        request_text = json.dumps(_messages(server.requests[0]), ensure_ascii=False)
        assert "<available_skills>" not in request_text
        listed = core.invoke_command(session_id, "skills", {}, expected_revision=core.command_catalog().revision_id)
        assert listed.output == "Skills are disabled."


def test_skill_reader_serves_unknown_and_disabled_through_real_read(tmp_path: Path) -> None:
    core = _core(tmp_path, None, skill_plugin_enabled=True, skills_enabled=True)
    entry = _write_skill(tmp_path / "workspace", name="release", description="Cut a release", body="steps")
    session_id = core.create_session().session_id
    context = ToolContext(
        workspace=tmp_path / "workspace",
        session_id=session_id,
        run_id="run-skill-reader",
        invocation_id="inv-skill-reader",
        revision_id=core.plugin_runtime.revision_id,
        actor="model",
    )
    served = core.plugin_runtime.invoke(ToolCall("read", {"path": "skill://release"}), context=context)
    assert isinstance(served, ToolSuccess)
    assert served.coverage == ()
    assert f"## Skill: release\n**Description**: Cut a release\nsourcePath: {entry}" in str(getattr(served.output, "text", ""))
    assert served.body is not None
    assert served.body.as_payload()["name"] == "release"

    missing = core.plugin_runtime.invoke(ToolCall("read", {"path": "skill://missing"}), context=context)
    assert isinstance(missing, ToolFailure)
    assert missing.error == "read cannot resolve resource URI skill://missing: no such resource"
