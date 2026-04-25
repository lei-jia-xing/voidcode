from __future__ import annotations

import pytest

from voidcode.command import (
    CommandDefinition,
    CommandRegistry,
    load_command_registry,
    load_markdown_commands,
    resolve_prompt_command,
    resolve_tool_instruction,
)
from voidcode.tools.contracts import ToolDefinition


def test_project_markdown_command_overrides_builtin_and_renders_arguments(tmp_path) -> None:
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


def test_load_markdown_commands_rejects_invalid_frontmatter(tmp_path) -> None:
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
