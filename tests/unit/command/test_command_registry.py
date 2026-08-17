from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.command import (
    CommandDefinition,
    CommandRegistry,
    builtin_commands,
    load_command_registry,
    load_markdown_commands,
    resolve_prompt_command,
    resolve_tool_instruction,
)
from voidcode.command.templating import render_command_template, split_command_arguments
from voidcode.tools.contracts import ToolDefinition


def test_project_markdown_command_overrides_builtin_and_renders_arguments(
    tmp_path: Path,
) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "review.md").write_text(
        "---\ndescription: Project review command\nagent: reviewer\n---\nReview $1 with context: $ARGUMENTS\n",
        encoding="utf-8",
    )

    registry = load_command_registry(workspace=tmp_path)
    command = registry.get("review")

    assert command is not None
    assert command.source == "project"
    assert command.agent == "reviewer"
    resolution = resolve_prompt_command('/review "src/app.py" carefully', registry)
    assert resolution is not None
    assert resolution.invocation.arguments == ("src/app.py", "carefully")
    assert resolution.invocation.rendered_prompt == ('Review src/app.py with context: "src/app.py" carefully')


def test_load_markdown_commands_rejects_invalid_frontmatter(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "bad.md").write_text(
        "---\nenabled: sometimes\n---\nNope\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="boolean frontmatter"):
        _ = load_markdown_commands(commands_dir, source="project")


def test_prompt_command_rejects_unknown_command() -> None:
    registry = CommandRegistry((CommandDefinition("known", "Known command", "Do $ARGUMENTS"),))

    with pytest.raises(ValueError, match="unknown command"):
        _ = resolve_prompt_command("/missing target", registry)


def test_tool_instruction_resolver_is_shared_for_read_grep_run_and_write() -> None:
    tools = (
        ToolDefinition("read", "Read", read_only=True),
        ToolDefinition("grep", "Grep", read_only=True),
        ToolDefinition("shell_exec", "Run", read_only=False),
        ToolDefinition("write", "Write", read_only=False),
    )

    assert resolve_tool_instruction("read sample.txt", tools, unavailable_message_suffix="test").tool_call.arguments == {"path": "sample.txt"}
    assert resolve_tool_instruction("grep hello src", tools, unavailable_message_suffix="test").tool_call.arguments == {
        "pattern": "hello",
        "path": "src",
    }
    assert resolve_tool_instruction("run pytest", tools, unavailable_message_suffix="test").tool_call.arguments == {"command": "pytest"}
    assert resolve_tool_instruction("write output.txt hello", tools, unavailable_message_suffix="test").tool_call.arguments == {
        "path": "output.txt",
        "content": "hello",
    }


def test_template_rendering_does_not_rewrite_inserted_arguments_or_dollar_literals() -> None:
    from voidcode.command.templating import render_command_template

    rendered = render_command_template(
        "Cost $100; first=$1; second=$2; missing=$3; args=$ARGUMENTS; literal=$ARGUMENTS_suffix",
        raw_arguments="price=$2 literal",
        arguments=("target",),
    )

    assert rendered == ("Cost $100; first=target; second=; missing=; args=price=$2 literal; literal=$ARGUMENTS_suffix")


class TestBuiltinCommandDiscovery:
    _EXPECTED_NAMES = (
        "init",
        "plan",
    )

    def test_expected_builtins_present(self) -> None:
        commands = builtin_commands()
        names = tuple(c.name for c in commands)
        assert names == self._EXPECTED_NAMES, f"expected {self._EXPECTED_NAMES}, got {names}"

    def test_every_builtin_has_source_and_template(self) -> None:
        for cmd in builtin_commands():
            assert cmd.source == "builtin", f"/{cmd.name} source should be builtin"
            assert cmd.template.strip(), f"/{cmd.name} template must be non-empty"
            assert "$ARGUMENTS" in cmd.template, f"/{cmd.name} template must contain $ARGUMENTS"
            assert cmd.description.strip(), f"/{cmd.name} description must be non-empty"
            assert cmd.enabled, f"/{cmd.name} must be enabled by default"

    def test_commands_registered_in_correct_order(self) -> None:
        ordered = [c.name for c in builtin_commands()]
        assert ordered == list(self._EXPECTED_NAMES), f"wrong order: {ordered}"

    def test_plan_command_targets_planning_mode_without_agent_switch(self) -> None:
        plan = [c for c in builtin_commands() if c.name == "plan"][0]
        assert plan.agent is None, f"/plan should keep the active agent, got {plan.agent}"
        assert plan.mode == "plan"

    def test_builtin_registry_can_read_modes_by_name(self) -> None:
        registry = CommandRegistry(builtin_commands())

        assert registry.get_mode("plan") == "plan"
        assert registry.get_mode("init") is None

    def test_commands_are_disabled_when_hidden_flag_set(self) -> None:
        registry = CommandRegistry(builtin_commands())
        hidden_cmd = CommandDefinition("hidden_cmd", "Hidden", "echo $ARGUMENTS", hidden=True)
        registry.register(hidden_cmd)
        visible = registry.list()
        assert all(c.name != "hidden_cmd" for c in visible)
        all_cmds = registry.list(include_hidden=True)
        assert any(c.name == "hidden_cmd" for c in all_cmds)

    def test_command_definition_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValueError, match="command mode must be 'normal' or 'plan'"):
            _ = CommandDefinition("cmd", "Command", "Do $ARGUMENTS", mode="banana")


class TestMarkdownCommandMode:
    def test_mode_frontmatter_is_loaded_and_resolved(self, tmp_path: Path) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "plan.md").write_text(
            "---\ndescription: Review this target\nmode: plan\n---\nReview $ARGUMENTS carefully\n",
            encoding="utf-8",
        )

        registry = load_command_registry(workspace=tmp_path)
        command = registry.get("plan")

        assert command is not None
        assert command.source == "project"
        assert command.mode == "plan"
        resolution = resolve_prompt_command("/plan src/app.py", registry)
        assert resolution is not None
        assert resolution.definition.mode == "plan"
        assert resolution.invocation.rendered_prompt == "Review src/app.py carefully"


class TestBuiltinCommandRendering:
    def test_init_renders_agents_md_generation_guidance(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "init"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="focus on runtime boundaries",
            arguments=split_command_arguments("focus on runtime boundaries"),
        )

        assert "focus on runtime boundaries" in rendered
        assert "Generate or refresh the project knowledge base (AGENTS.md)" in rendered
        assert "WHERE TO LOOK" in rendered
        assert "CODE MAP" in rendered
        assert "Never store secrets" in rendered
        assert "read the final AGENTS.md" in rendered

    def test_plan_renders_no_code_guidance(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "plan"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="add dark mode support",
            arguments=split_command_arguments("add dark mode support"),
        )
        assert "add dark mode support" in rendered
        assert "Produce an implementation plan before writing code" in rendered
        assert "acceptance criteria" in rendered
        assert "do not write code or modify files" in rendered
        assert "delegate to the product agent (subagent_type=product)" in rendered
        assert "todo_write is runtime state" in rendered

    def test_dollar_placeholder_substitution_uses_shlex_splitting(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "plan"][0]
        args = split_command_arguments('"path with spaces/file.py" --flag')
        rendered = render_command_template(
            cmd.template,
            raw_arguments='"path with spaces/file.py" --flag',
            arguments=args,
        )
        assert "path with spaces/file.py" in rendered
        assert args == ("path with spaces/file.py", "--flag")


class TestBuiltinCommandProjectOverride:
    def test_project_plan_overrides_builtin_plan(self, tmp_path: Path) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "plan.md").write_text(
            "---\ndescription: Custom project plan command\nagent: worker\n---\nScope $1 and return a short plan\n",
            encoding="utf-8",
        )

        registry = load_command_registry(workspace=tmp_path)
        cmd = registry.get("plan")
        assert cmd is not None
        assert cmd.source == "project"
        assert cmd.agent == "worker"
        resolution = resolve_prompt_command("/plan the login flow", registry)
        assert resolution is not None
        assert resolution.invocation.arguments == ("the", "login", "flow")
        assert "Scope the" in resolution.invocation.rendered_prompt
        assert "short plan" in resolution.invocation.rendered_prompt

    def test_project_override_preserves_other_builtins(self, tmp_path: Path) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "plan.md").write_text(
            "---\ndescription: Custom plan\n---\nPlan $ARGUMENTS\n",
            encoding="utf-8",
        )

        registry = load_command_registry(workspace=tmp_path)
        plan_cmd = registry.get("plan")
        assert plan_cmd is not None
        assert plan_cmd.source == "project"
        for name in ("init",):
            cmd = registry.get(name)
            assert cmd is not None, f"{name} should still be registered"
            assert cmd.source == "builtin", f"/{name} source should still be builtin, got {cmd.source}"

    def test_project_disabled_command_not_listed(self, tmp_path: Path) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "plan.md").write_text(
            "---\ndescription: Disabled plan\nenabled: false\n---\nPlan $ARGUMENTS\n",
            encoding="utf-8",
        )

        registry = load_command_registry(workspace=tmp_path)
        cmd = registry.get("plan")
        assert cmd is not None
        assert not cmd.enabled
        visible = registry.list()
        assert not any(c.name == "plan" for c in visible)

    def test_nonexistent_slash_command_still_raises(self, tmp_path: Path) -> None:
        registry = load_command_registry(workspace=tmp_path)
        with pytest.raises(ValueError, match="unknown command"):
            _ = resolve_prompt_command("/nonexistent target", registry)
