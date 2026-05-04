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
        "---\n"
        "description: Project review command\n"
        "agent: reviewer\n"
        "---\n"
        "Review $1 with context: $ARGUMENTS\n",
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
    assert resolution.invocation.rendered_prompt == (
        'Review src/app.py with context: "src/app.py" carefully'
    )


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
        ToolDefinition("read_file", "Read", read_only=True),
        ToolDefinition("grep", "Grep", read_only=True),
        ToolDefinition("shell_exec", "Run", read_only=False),
        ToolDefinition("write_file", "Write", read_only=False),
    )

    assert resolve_tool_instruction(
        "read sample.txt", tools, unavailable_message_suffix="test"
    ).tool_call.arguments == {"filePath": "sample.txt"}
    assert resolve_tool_instruction(
        "grep hello src", tools, unavailable_message_suffix="test"
    ).tool_call.arguments == {"pattern": "hello", "path": "src"}
    assert resolve_tool_instruction(
        "run pytest", tools, unavailable_message_suffix="test"
    ).tool_call.arguments == {"command": "pytest"}
    assert resolve_tool_instruction(
        "write output.txt hello", tools, unavailable_message_suffix="test"
    ).tool_call.arguments == {"path": "output.txt", "content": "hello"}


def test_template_rendering_does_not_rewrite_inserted_arguments_or_dollar_literals() -> None:
    from voidcode.command.templating import render_command_template

    rendered = render_command_template(
        "Cost $100; first=$1; second=$2; missing=$3; args=$ARGUMENTS; literal=$ARGUMENTS_suffix",
        raw_arguments="price=$2 literal",
        arguments=("target",),
    )

    assert rendered == (
        "Cost $100; first=target; second=; missing=; "
        "args=price=$2 literal; literal=$ARGUMENTS_suffix"
    )


class TestBuiltinCommandDiscovery:
    _EXPECTED_NAMES = (
        "commit",
        "explain",
        "fix",
        "plan",
        "start-work",
        "continuation-loop",
        "intensive-loop",
        "cancel-continuation",
        "review",
        "test",
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

    def test_plan_command_targets_product_agent(self) -> None:
        plan = [c for c in builtin_commands() if c.name == "plan"][0]
        assert plan.agent == "product", f"/plan agent should be product, got {plan.agent}"
        assert plan.workflow_preset == "review"

    def test_start_work_command_targets_implementation_workflow(self) -> None:
        start_work = [c for c in builtin_commands() if c.name == "start-work"][0]

        assert start_work.agent is None
        assert start_work.workflow_preset == "implementation"

    def test_continuation_loop_commands_target_runtime_owned_flow(self) -> None:
        commands = {c.name: c for c in builtin_commands()}

        assert commands["continuation-loop"].workflow_preset == "implementation"
        assert commands["intensive-loop"].workflow_preset == "implementation"
        assert commands["cancel-continuation"].workflow_preset is None
        assert "runtime-owned" in commands["continuation-loop"].template
        assert "intensive=true" in commands["intensive-loop"].template
        assert "verification_status" in commands["intensive-loop"].template
        assert "latest active loop" in commands["cancel-continuation"].template

    def test_commands_are_disabled_when_hidden_flag_set(self) -> None:
        registry = CommandRegistry(builtin_commands())
        hidden_cmd = CommandDefinition("hidden_cmd", "Hidden", "echo $ARGUMENTS", hidden=True)
        registry.register(hidden_cmd)
        visible = registry.list()
        assert all(c.name != "hidden_cmd" for c in visible)
        all_cmds = registry.list(include_hidden=True)
        assert any(c.name == "hidden_cmd" for c in all_cmds)


class TestBuiltinCommandRendering:
    def test_fix_renders_arguments_correctly(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "fix"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="the null pointer bug in utils.py",
            arguments=split_command_arguments("the null pointer bug in utils.py"),
        )
        assert "null pointer bug in utils.py" in rendered
        assert "root cause" in rendered
        assert "smallest safe code change" in rendered
        assert "run targeted tests" in rendered
        assert "$ARGUMENTS" not in rendered

    def test_explain_renders_read_only_guidance(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "explain"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="the auth.py module",
            arguments=split_command_arguments("the auth.py module"),
        )
        assert "auth.py module" in rendered
        assert "do not modify any files" in rendered
        assert "do not hallucinate" in rendered

    def test_plan_renders_no_code_guidance(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "plan"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="add dark mode support",
            arguments=split_command_arguments("add dark mode support"),
        )
        assert "dark mode support" in rendered
        assert "do not write code" in rendered
        assert "Target agent: product" in rendered
        assert "acceptance criteria" in rendered
        assert "Use todo_write only for session planning/progress state" in rendered
        assert "Start-work handoff" in rendered

    def test_start_work_renders_handoff_guidance(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "start-work"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="plan from session abc",
            arguments=split_command_arguments("plan from session abc"),
        )

        assert "plan from session abc" in rendered
        assert "accepted plan or handoff" in rendered
        assert "Use todo_write for multi-step progress tracking" in rendered
        assert "run targeted checks" in rendered

    def test_commit_renders_conventional_commits_guidance(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "commit"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="ci pipeline fixes",
            arguments=split_command_arguments("ci pipeline fixes"),
        )
        assert "ci pipeline fixes" in rendered
        assert "Conventional Commits" in rendered
        assert "do not create commits" in rendered
        assert "no staged or unstaged changes" in rendered

    def test_test_renders_verification_guidance(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "test"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="src/auth.py",
            arguments=split_command_arguments("src/auth.py"),
        )
        assert "src/auth.py" in rendered
        assert "verification" in rendered.lower()
        assert "do not delete or weaken existing tests" in rendered
        assert "no test framework" in rendered

    def test_review_renders_arguments_and_severity(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "review"][0]
        rendered = render_command_template(
            cmd.template,
            raw_arguments="src/app.py",
            arguments=split_command_arguments("src/app.py"),
        )
        assert "src/app.py" in rendered
        assert "severity" in rendered
        assert "Read-only by default" in rendered
        assert "unreadable" in rendered

    def test_dollar_placeholder_substitution_uses_shlex_splitting(self) -> None:
        cmd = [c for c in builtin_commands() if c.name == "fix"][0]
        args = split_command_arguments('"path with spaces/file.py" --flag')
        rendered = render_command_template(
            cmd.template,
            raw_arguments='"path with spaces/file.py" --flag',
            arguments=args,
        )
        assert "path with spaces/file.py" in rendered
        assert args == ("path with spaces/file.py", "--flag")


class TestBuiltinCommandProjectOverride:
    def test_project_fix_overrides_builtin_fix(self, tmp_path: Path) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "fix.md").write_text(
            "---\n"
            "description: Custom project fix command\n"
            "agent: worker\n"
            "---\n"
            "Apply a targeted fix for $1 and verify with tests\n",
            encoding="utf-8",
        )

        registry = load_command_registry(workspace=tmp_path)
        cmd = registry.get("fix")
        assert cmd is not None
        assert cmd.source == "project"
        assert cmd.agent == "worker"
        resolution = resolve_prompt_command("/fix the login timeout bug", registry)
        assert resolution is not None
        assert "targeted fix" in resolution.invocation.rendered_prompt
        assert "verify with tests" in resolution.invocation.rendered_prompt

    def test_project_override_preserves_other_builtins(self, tmp_path: Path) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "fix.md").write_text(
            "---\ndescription: Custom fix\n---\nFix $ARGUMENTS\n",
            encoding="utf-8",
        )

        registry = load_command_registry(workspace=tmp_path)
        fix_cmd = registry.get("fix")
        assert fix_cmd is not None
        assert fix_cmd.source == "project"
        for name in ("review", "explain", "plan", "start-work", "test", "commit"):
            cmd = registry.get(name)
            assert cmd is not None, f"{name} should still be registered"
            assert cmd.source == "builtin", (
                f"/{name} source should still be builtin, got {cmd.source}"
            )

    def test_project_disabled_command_not_listed(self, tmp_path: Path) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "fix.md").write_text(
            "---\ndescription: Disabled fix\nenabled: false\n---\nFix $ARGUMENTS\n",
            encoding="utf-8",
        )

        registry = load_command_registry(workspace=tmp_path)
        cmd = registry.get("fix")
        assert cmd is not None
        assert not cmd.enabled
        visible = registry.list()
        assert not any(c.name == "fix" for c in visible)

    def test_nonexistent_slash_command_still_raises(self, tmp_path: Path) -> None:
        registry = load_command_registry(workspace=tmp_path)
        with pytest.raises(ValueError, match="unknown command"):
            _ = resolve_prompt_command("/nonexistent target", registry)
