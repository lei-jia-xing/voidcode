"""Declarative command help projections and their terminal layout."""

from __future__ import annotations

from voidcode.sdk.command_help import (
    command_argument_hints,
    command_usage,
    grouped_help_entries,
    help_entries,
    public_command_path,
)
from voidcode.sdk.commands import CommandDefinition, CommandDescriptor
from voidcode.tui.help_view import TUI_CONTROLS, render_help
from voidcode.tui.styles import Styles
from voidcode.tui.term import visible_width


def _descriptor(name: str, owner: str, description: str = "Description", **kwargs: object) -> CommandDescriptor:
    return CommandDescriptor(name=name, owner=owner, description=description, **kwargs)  # type: ignore[arg-type]


def test_public_path_and_usage_derive_from_descriptor_fields() -> None:
    descriptor = _descriptor(
        "goal.set",
        "goal",
        "Set a goal.",
        argument_schema={
            "type": "object",
            "properties": {"objective": {"type": "string"}, "token_budget": {"type": "integer"}},
            "required": ["objective"],
        },
        examples=("/goal set Ship it",),
        group="Goal",
    )
    assert public_command_path(descriptor) == "goal set"
    assert command_argument_hints(descriptor) == ("<objective>", "[token_budget]")
    assert command_usage(descriptor) == "/goal set <objective> [token_budget]"

    explicit = _descriptor("shell", "user_shell", usage="/shell <command>  ·  !<command>", client_syntax="!")
    assert command_usage(explicit) == "/shell <command>  ·  !<command>"


def test_help_entries_filter_sort_and_group() -> None:
    descriptors = (
        _descriptor("zeta", "plugin", "Z"),
        _descriptor("internal", "plugin", "Hidden", slash_visible=False),
        _descriptor("core.a", "core", "A"),
        _descriptor("beta", "core", "B"),
    )
    entries = help_entries(descriptors)
    assert [entry.name for entry in entries] == ["beta", "core.a", "zeta"]
    assert [group for group, _entries in grouped_help_entries(descriptors)] == ["Core", "plugin"]


def test_metadata_round_trips_through_the_catalog_payload() -> None:
    descriptor = _descriptor(
        "shell",
        "user_shell",
        usage="/shell <command>",
        examples=("/shell git status",),
        group="Shell",
        argument_schema={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    )
    decoded = CommandDescriptor.from_payload(descriptor.as_payload())
    assert decoded == descriptor
    definition = CommandDefinition(
        name="shell",
        description="Run a shell command.",
        usage="/shell <command>",
        examples=("/shell true",),
        group="Shell",
    )
    assert definition.examples == ("/shell true",)


def _rows(width: int, descriptors: tuple[CommandDescriptor, ...]) -> list[str]:
    return render_help(
        descriptors,
        revision="rev-123456789",
        width=width,
        styles=Styles(None),
        bindings={},
        conflicts={},
        quit_arm_seconds=1.5,
    )


def test_help_fits_narrow_and_wide_terminals_without_losing_content() -> None:
    descriptors = (
        _descriptor("run", "core", "Run one prompt and continue the session."),
        _descriptor(
            "goal.set",
            "goal",
            "Create a new goal or replace an existing goal with a deliberately long explanation.",
            argument_schema={
                "type": "object",
                "properties": {"objective": {"type": "string"}, "token_budget": {"type": "integer"}},
                "required": ["objective"],
            },
        ),
        _descriptor("workflow.status", "workflows", "Read one workflow run's graph and bounded node results.", client_syntax="/workflow status"),
        _descriptor("wide", "plugin", "宽字符描述" * 12),
    )
    for width in (40, 80, 120):
        rows = _rows(width, descriptors)
        assert all(visible_width(row) <= width for row in rows), (width, [(row, visible_width(row)) for row in rows if visible_width(row) > width])
        joined = "\n".join(rows)
        assert "/goal set <objective>" in joined
        assert "[token_budget]" in joined
        assert "宽字符描述" in joined


def test_help_columns_are_group_local_at_120() -> None:
    """A long command in one group must not push another group's descriptions right."""
    descriptors = (
        _descriptor("help", "core", "Show available commands and documentation."),
        _descriptor("status", "core", "Show workspace status."),
        _descriptor(
            "shell",
            "user_shell",
            "Run a shell command locally.",
            client_syntax="!",
            usage="/shell <command>  ·  !<command>",
            group="Shell",
        ),
        _descriptor(
            "plugin.extremely_long_representative_command_name_for_narrow_layout",
            "demo_plugin",
            "A long dotted command name.",
            argument_schema={"type": "object", "properties": {"first_argument": {"type": "string"}}, "required": ["first_argument"]},
            group="Demo plugin",
        ),
    )
    rows = _rows(120, descriptors)
    assert all(visible_width(row) <= 120 for row in rows)
    help_row = next(row for row in rows if "/help" in row)
    shell_row = next(row for row in rows if "Run a shell command" in row)
    help_column = help_row.index("Show available")
    shell_column = shell_row.index("Run a shell command")
    assert help_column < 60
    assert shell_column != help_column
    assert any(row.strip().startswith("/plugin extremely_long") for row in rows)
    assert any(row.strip() == "A long dotted command name." for row in rows)


def test_help_groups_by_owner_and_lists_shortcuts() -> None:
    descriptors = (
        _descriptor("goal", "goal", "Show the goal."),
        _descriptor("run", "core", "Run a prompt."),
        _descriptor("monitor", "subagents", "Open the monitor.", default_shortcut="alt+a"),
    )
    rows = render_help(
        descriptors,
        revision="rev-123456789",
        width=100,
        styles=Styles(None),
        bindings={"alt+a": descriptors[2]},
        conflicts={},
        quit_arm_seconds=1.5,
    )
    text = "\n".join(rows)
    assert "Core" in text and "goal" in text and "subagents" in text
    assert "alt+a" in text
    assert text.index("Core") < text.index("goal") < text.index("subagents")


def test_plugin_text_is_escaped_and_local_controls_are_listed() -> None:
    rows = _rows(80, (_descriptor("danger", "plugin", "boom \x1b[31mX"),))
    text = "\n".join(rows)
    assert "\x1b" not in text
    assert "\\x1b[31mX" in text
    for control in TUI_CONTROLS:
        assert f"/tui {control.usage}" in text


def test_catalog_failure_still_renders_local_controls() -> None:
    rows = render_help(
        (),
        revision="",
        width=80,
        styles=Styles(None),
        bindings={},
        conflicts={},
        quit_arm_seconds=1.5,
        catalog_error="server unavailable",
    )
    text = "\n".join(rows)
    assert "Catalog unavailable: server unavailable (retry)" in text
    assert "/tui help" in text
    assert "No commands are admitted" not in text


def test_shortcut_conflicts_are_explicit() -> None:
    first = _descriptor("monitor", "subagents", "Monitor", default_shortcut="alt+a")
    second = _descriptor("other", "plugin", "Other", default_shortcut="alt+a")
    rows = render_help(
        (first, second),
        revision="r",
        width=100,
        styles=Styles(None),
        bindings={},
        conflicts={"alt+a": ("monitor", "other")},
        quit_arm_seconds=1.5,
    )
    assert "conflicts: /monitor, /other (unbound)" in "\n".join(rows)
