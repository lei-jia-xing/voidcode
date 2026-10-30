"""Subagent files use Claude Code's names, body, paths and executable options."""

from importlib.resources import files

import pytest

from voidcode.plugins.delegation import create_plugin
from voidcode.plugins.delegation.profiles.builtin import list_builtin_agents
from voidcode.plugins.delegation.profiles.commands import AgentsCommand, AgentShowCommand
from voidcode.plugins.delegation.profiles.registry import agent_from_markdown, load_agent_registry, project_agent_dir, user_agent_dir
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext


def _document(name="helper", extra="", body="Help locally."):
    return f"---\nname: {name}\ndescription: Local helper\n{extra}---\n{body}"


def _write(directory, filename, text):
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_minimal_standard_definition_uses_name_and_body_without_extra_manifest_fields():
    agent = agent_from_markdown(_document("Exact Name"), scope="project")
    assert agent.name == "Exact Name" and agent.prompt == "Help locally."
    assert agent.tools is None and agent.max_turns is None and not agent.readonly


@pytest.mark.parametrize("tools", ["tools: read, grep\n", "tools: Read, Grep\n", "tools: [read, grep]\n", "tools:\n  - read\n  - grep\n"])
def test_standard_tools_and_turn_cap_are_parsed(tools):
    agent = agent_from_markdown(_document(extra=tools + "model: inherit\nmaxTurns: 4\n"), scope="user")
    assert agent.tools == ("read", "grep") and agent.max_turns == 4


@pytest.mark.parametrize(
    "extra,match",
    [
        ("model: sonnet\n", "per-agent model selection"),
        ("permissionMode: bypassPermissions\n", "unsupported agent field"),
        ("tools: []\n", "agent tools"),
        ("tools: read,,grep\n", "agent tools"),
        ("maxTurns: true\n", "positive integer"),
        ("maxTurns: 0\n", "positive integer"),
        ("mode: subagent\n", "unsupported agent field"),
        ("id: fake\n", "unsupported agent field"),
        ("fallback_models: [other]\n", "unsupported agent field"),
        ("skill_refs: [unused]\n", "unsupported agent field"),
        ("prompt_append: text\n", "unsupported agent field"),
        ("tool_allowlist: [read]\n", "unsupported agent field"),
    ],
)
def test_unimplemented_or_invalid_fields_fail_instead_of_being_ignored(extra, match):
    with pytest.raises(ValueError, match=match):
        agent_from_markdown(_document(extra=extra), scope="project")


@pytest.mark.parametrize(
    "text,match",
    [
        ("no metadata", "must start"),
        (_document(body=" "), "empty markdown body"),
        ("---\nname: one\nname: two\ndescription: d\n---\nbody", "duplicate frontmatter"),
        ("---\nname: 7\ndescription: d\n---\nbody", "name.*non-empty string"),
        ("---\nname: one\n---\nbody", "description.*non-empty string"),
        (_document("reserved:name"), "contain no ':'"),
    ],
)
def test_invalid_metadata_and_framing_are_visible(text, match):
    with pytest.raises(ValueError, match=match):
        agent_from_markdown(text, scope="project")


def test_project_overrides_user_and_builtin_and_discovers_nested_files(tmp_path):
    workspace = tmp_path / "workspace"
    user = tmp_path / "user"
    assert user_agent_dir({"CLAUDE_CONFIG_DIR": str(user)}) == user / "agents"
    assert project_agent_dir(workspace) == workspace.resolve() / ".claude/agents"
    _write(user / "agents", "different-filename.md", _document("helper", body="user body"))
    _write(project_agent_dir(workspace), "nested/work.md", _document("helper", body="project body"))
    _write(project_agent_dir(workspace), "override.md", _document("explore", body="custom explore"))
    registry = load_agent_registry(workspace, env={"CLAUDE_CONFIG_DIR": str(user)})
    assert registry.get("helper").prompt == "project body"
    assert registry.get("helper").source_scope == "project"
    assert registry.get("explore").prompt == "custom explore"
    assert registry.get("missing") is None


def test_duplicates_in_one_scope_and_malformed_files_report_paths(tmp_path):
    project = project_agent_dir(tmp_path)
    first = _write(project, "one.md", _document())
    second = _write(project, "nested/two.md", _document())
    with pytest.raises(ValueError, match="duplicate agent name") as error:
        load_agent_registry(tmp_path, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "user")})
    assert str(first) in str(error.value) and str(second) in str(error.value)
    second.write_text("invalid")
    with pytest.raises(ValueError, match="invalid agent") as error:
        load_agent_registry(tmp_path, env={"CLAUDE_CONFIG_DIR": str(tmp_path / "user")})
    assert str(second) in str(error.value)


def test_builtin_prompts_and_stances_are_preserved():
    resources = files("voidcode.plugins.delegation.profiles").joinpath("texts")
    for agent in list_builtin_agents():
        assert agent.prompt == resources.joinpath(f"{agent.name}.txt").read_text().strip()
        assert agent.tools is None
        assert agent.readonly == (agent.name in {"review", "explore", "research", "plan"})


def test_agent_commands_report_actual_definition(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "user"))
    _write(project_agent_dir(tmp_path), "local.md", _document(extra="maxTurns: 3\n"))
    context = ToolContext(tmp_path, "session", "run", "invocation", "revision", "user")
    shown = AgentShowCommand().invoke(CommandInvocation(context, "agents.show", {"name": "helper"}), None)
    assert shown.status == "ok" and shown.output == "Help locally."
    assert shown.data["name"] == "helper" and shown.data["maxTurns"] == 3
    assert "model_preference" not in shown.data and "prompt_materialization" not in shown.data
    listed = AgentsCommand().invoke(CommandInvocation(context, "agents", {}), None)
    assert listed.status == "ok" and "helper" in listed.output
    assert listed.presentation.view_type == "choices"
    assert listed.presentation.data["action"] == "agents.show"
    assert any(option["id"] == "helper" for option in listed.presentation.data["options"])
    assert shown.presentation.view_type == "markdown"
    assert shown.presentation.data["content"] == shown.output
    unknown = AgentShowCommand().invoke(CommandInvocation(context, "agents.show", {"name": "missing"}), None)
    assert unknown.status == "error"
    assert [tool.definition.name for tool in create_plugin({}).tools] == ["task", "task_batch"]


def test_agent_input_discovers_names_without_executing_show(tmp_path, monkeypatch):
    from voidcode.sdk.views import FormViewData, validate_form_submission

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "user"))
    _write(project_agent_dir(tmp_path), "custom.md", _document())
    context = ToolContext(tmp_path, "session", "run", "invocation", "revision", "user")
    command = AgentShowCommand()
    presentation = command.prepare_input(CommandInvocation(context, "agents.show"), None)
    form = FormViewData.from_payload(presentation.data)
    assert form.action == "agents.show"
    assert any(option.id == "helper" for option in form.fields[0].options)
    args = validate_form_submission(form, {"name": "helper"})
    shown = command.invoke(CommandInvocation(context, form.action, args), None)
    assert shown.output == "Help locally."
