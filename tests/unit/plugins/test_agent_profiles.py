"""Subagent files use Claude Code's names, body, paths and executable options."""

from importlib.resources import files

import pytest

from voidcode.plugins.subagents import create_plugin
from voidcode.plugins.subagents.profiles.builtin import list_builtin_agents
from voidcode.plugins.subagents.profiles.registry import agent_from_markdown, load_agent_registry, project_agent_dir, user_agent_dir


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
    assert agent.max_turns is None and not agent.readonly


def test_standard_turn_cap_is_parsed():
    agent = agent_from_markdown(_document(extra="model: inherit\nmaxTurns: 4\n"), scope="user")
    assert agent.max_turns == 4


@pytest.mark.parametrize(
    "extra,match",
    [
        ("model: sonnet\n", "per-agent model selection"),
        ("permissionMode: bypassPermissions\n", "unsupported agent field"),
        ("tools: [read, grep]\n", "unsupported agent field"),
        ("tools: read, grep\n", "unsupported agent field"),
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
    resources = files("voidcode.plugins.subagents.profiles").joinpath("texts")
    agents = list_builtin_agents()
    assert {agent.name for agent in agents} == {"execute", "review", "explore", "research", "plan"}
    for agent in agents:
        assert agent.prompt == resources.joinpath(f"{agent.name}.txt").read_text().strip()
        assert agent.readonly == (agent.name in {"review", "explore", "research", "plan"})


def test_subagents_plugin_surface():
    plugin = create_plugin({})
    assert [tool.definition.name for tool in plugin.tools] == ["task", "task_batch"]
    assert [cmd.definition.name for cmd in plugin.commands] == ["subagents.monitor", "subagents.monitor.cancel"]
