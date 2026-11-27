"""Declarative help layout for the inline terminal client.

Everything here is a pure projection of the command catalog plus the client's
own controls. There is deliberately no command list in this module: commands
come from :class:`~voidcode.sdk.commands.CommandDescriptor`; the only local
table is the client-owned ``/tui`` namespace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from voidcode.sdk.client import ClientCapabilities, ClientCapability
from voidcode.sdk.command_help import CommandHelpEntry, command_usage, grouped_help_entries
from voidcode.sdk.commands import CommandDescriptor
from voidcode.tui.styles import Styles
from voidcode.tui.term import escape_terminal_controls, visible_width, wrap_plain

__all__ = [
    "TUI_CONTROLS",
    "TUI_CONTROLS_BY_NAME",
    "TuiControl",
    "render_help",
]


@dataclass(frozen=True, slots=True)
class TuiControl:
    """One client-owned ``/tui`` control, shared by dispatch, completion, and help."""

    name: str
    usage: str
    summary: str
    aliases: tuple[str, ...] = ()
    #: Capability that must be declared for the mutating form to work. Bare
    #: forms may still read state; dispatch refuses the mutation visibly.
    requires: ClientCapability | None = None


#: The client reserves ``/tui``; these are not core commands and never appear in
#: the command catalog. This table is the single source for their discovery.
TUI_CONTROLS: tuple[TuiControl, ...] = (
    TuiControl("help", "help", "Show this help."),
    TuiControl("output", "output", "View the latest truncated tool output.", aliases=("artifact",)),
    TuiControl("sessions", "sessions", "List available sessions."),
    TuiControl("session", "session <id>", "Switch the active session."),
    TuiControl("new", "new", "Create a new session."),
    TuiControl("views", "views [name]", "Open plugin views or forms."),
    TuiControl("attach", "attach <path> [path...]", "Attach image file(s) to the next prompt."),
    TuiControl("image", "image", "Attach an image from the system clipboard.", aliases=("paste-image",)),
    TuiControl("detach", "detach [index|all]", "Remove pending image attachments."),
    TuiControl("model", "model [provider/model]", "Show or switch the model.", requires=ClientCapability.MODEL_SWITCH),
    TuiControl("thinking", "thinking [level|off]", "Show or set the thinking level.", requires=ClientCapability.THINKING_SWITCH),
    TuiControl("cancel", "cancel", "Cancel the active run."),
    TuiControl("quit", "quit", "Exit the TUI.", aliases=("exit",)),
)

TUI_CONTROLS_BY_NAME: Mapping[str, TuiControl] = {name: control for control in TUI_CONTROLS for name in (control.name, *control.aliases)}


def _clean(value: str) -> str:
    """One-line, control-free plugin/server text for help rows."""
    return escape_terminal_controls(" ".join(value.splitlines()), preserve_tabs=False)


def _heading(text: str, styles: Styles) -> str:
    return styles.render("accent", _clean(text), bold=True)


def _dim(text: str, styles: Styles) -> str:
    return styles.fg("muted", _clean(text))


def _usage(text: str, styles: Styles) -> str:
    return styles.render("text", _clean(text), bold=True)


def _table_rows(
    items: list[tuple[str, str, tuple[str, ...]]],
    *,
    width: int,
    indent: int,
    styles: Styles,
    usage_width: int | None = None,
) -> list[str]:
    """Two-column usage/summary table with a hanging description indent.

    A section uses one usage column. When a usage line exceeds the cap, that
    row stacks instead of shrinking every description. Width < 56 always stacks.
    """
    if not items:
        return []
    max_usage = max(visible_width(usage) for usage, _summary, _examples in items)
    column_width = usage_width if usage_width is not None else min(max_usage, max(14, width - 38))
    desc_column = indent + column_width + 2
    table = width >= 56 and desc_column + 20 <= width
    rows: list[str] = []
    for usage, summary, examples in items:
        clean_usage = _clean(usage) or "command"
        clean_summary = _clean(summary) or "(no description)"
        stacked = not table or visible_width(clean_usage) > column_width
        if stacked:
            for index, line in enumerate(wrap_plain(clean_usage, max(1, width - indent - 2))):
                rows.append(" " * (indent if index == 0 else indent + 2) + _usage(line, styles))
            description_rows = wrap_plain(clean_summary, max(1, width - indent - 2))
            rows.extend(" " * (indent + 2) + _dim(line, styles) for line in description_rows)
            example_indent = indent + 4
        else:
            pad = " " * (column_width - visible_width(clean_usage))
            description_width = max(1, width - desc_column)
            description_rows = wrap_plain(clean_summary, description_width)
            rows.append(" " * indent + _usage(clean_usage, styles) + pad + "  " + _dim(description_rows[0], styles))
            rows.extend(" " * desc_column + _dim(line, styles) for line in description_rows[1:])
            example_indent = desc_column
        for example in examples:
            clean_example = _clean(example)
            if not clean_example:
                continue
            for line in wrap_plain(f"e.g. {clean_example}", max(1, width - example_indent)):
                rows.append(" " * example_indent + _dim(line, styles))
    return rows


def _command_rows(
    groups: tuple[tuple[str, tuple[CommandHelpEntry, ...]], ...],
    *,
    width: int,
    styles: Styles,
) -> list[str]:
    rows: list[str] = []
    for group, entries in groups:
        # Each root starts a row; its subcommands are indented beneath it while
        # keeping the full `/root subcommand` spelling in the usage column.
        items = [("  " * (entry.depth - 1) + entry.usage, entry.description, entry.examples) for entry in entries]
        for line in wrap_plain(group, max(1, width - 2)):
            rows.append("  " + _heading(line, styles))
        rows.extend(_table_rows(items, width=width, indent=4, styles=styles))
    return rows


def _key_rows(
    bindings: Mapping[str, CommandDescriptor],
    conflicts: Mapping[str, tuple[str, ...]],
    *,
    width: int,
    styles: Styles,
    quit_arm_seconds: float,
) -> list[str]:
    fixed = [
        ("Esc", "Cancel a run, or close or deny a dialog."),
        ("Ctrl+C", f"Clear the draft; press twice within {quit_arm_seconds:g}s to quit."),
        ("Ctrl+D", "Quit when the draft is empty."),
        ("Ctrl+O", "Open the latest tool artifact or truncated output."),
        ("Alt+Up", "Withdraw the last queued follow-up into the composer for editing."),
        ("Alt+I", "Attach image file(s) by path; the draft and caret stay intact."),
        ("Alt+V", "Attach an image from the system clipboard."),
        ("Alt+R", "Remove the last pending image attachment."),
    ]
    rows: list[tuple[str, str]] = [(chord, summary) for chord, summary in fixed]
    for chord in sorted(bindings):
        descriptor = bindings[chord]
        if descriptor.slash_visible:
            summary = f"{command_usage(descriptor)} — {descriptor.description}"
        else:
            summary = descriptor.description
        rows.append((chord, summary))
    for chord in sorted(conflicts):
        claimants = ", ".join(f"/{_clean(name).replace('.', ' ')}" for name in conflicts[chord])
        rows.append((chord, f"conflicts: {claimants} (unbound)"))

    chord_width = max((visible_width(chord) for chord, _summary in rows), default=0)
    table = 2 + chord_width + 2 + 12 <= width
    out: list[str] = []
    for chord, summary in rows:
        clean_chord = _clean(chord)
        clean_summary = _clean(summary)
        is_conflict = chord in conflicts
        summary_style = "warning" if is_conflict else None
        if not table:
            for line in wrap_plain(clean_chord, max(1, width - 2)):
                out.append("  " + _usage(line, styles))
            for line in wrap_plain(clean_summary, max(1, width - 4)):
                out.append("    " + (styles.fg(summary_style, line) if summary_style else _dim(line, styles)))
            continue
        pad = " " * max(0, chord_width - visible_width(clean_chord))
        prefix = "  " + _usage(clean_chord, styles) + pad + "  "
        continuation = " " * (2 + chord_width + 2)
        wrapped = wrap_plain(clean_summary, max(1, width - len(continuation)))
        out.append(prefix + (styles.fg(summary_style, wrapped[0]) if summary_style else _dim(wrapped[0], styles)))
        for line in wrapped[1:]:
            out.append(continuation + (styles.fg(summary_style, line) if summary_style else _dim(line, styles)))
    return out


def _control_rows(width: int, styles: Styles, capabilities: ClientCapabilities) -> list[str]:
    #: Gated controls are embedded-derived today; the capability remains the
    #: single source, so help cannot disagree with dispatch.
    items: list[tuple[str, str, tuple[str, ...]]] = [
        (
            f"/tui {control.usage}",
            control.summary + (" (embedded only)" if control.requires is not None and not capabilities.supports(control.requires) else ""),
            (),
        )
        for control in TUI_CONTROLS
    ]
    return _table_rows(items, width=width, indent=2, styles=styles)


def render_help(
    descriptors: tuple[CommandDescriptor, ...],
    *,
    revision: str,
    width: int,
    styles: Styles,
    bindings: Mapping[str, CommandDescriptor],
    conflicts: Mapping[str, tuple[str, ...]],
    quit_arm_seconds: float,
    capabilities: ClientCapabilities,
    catalog_error: str | None = None,
) -> list[str]:
    """Render the complete terminal help screen as committed scrollback rows.

    The caller owns catalog acquisition and commits these rows exactly once.
    ``catalog_error`` renders a warning and still shows the client controls.
    """
    width = max(1, width)
    groups = grouped_help_entries(descriptors)
    visible = sum(len(entries) for _group, entries in groups)
    meta = [f"{visible} shown"]
    revision_text = _clean(revision)
    if width >= 56 and revision_text:
        if revision_text.lower().startswith("rev"):
            meta.insert(0, revision_text)
        else:
            meta.insert(0, f"rev {revision_text[:8]}")
    header_plain = "VoidCode commands"
    meta_text = "  ·  ".join(meta)
    if visible_width(header_plain) + visible_width(meta_text) + 3 <= width:
        header_plain = f"{header_plain}  ·  {meta_text}"
    rows = [styles.render("accent", line, bold=True) for line in wrap_plain(header_plain, width)]
    if catalog_error:
        for line in wrap_plain(f"Catalog unavailable: {_clean(catalog_error)} (retry)", max(1, width - 2)):
            rows.append("  " + styles.fg("warning", line))
    rows.append("")
    rows.append(_heading("Commands", styles))
    if groups:
        rows.extend(_command_rows(groups, width=width, styles=styles))
    elif not catalog_error:
        for line in wrap_plain("No commands are admitted. Enable a plugin in voidcode.toml.", max(1, width - 2)):
            rows.append("  " + styles.fg("warning", line))
    rows.extend(["", _heading("Keys", styles)])
    rows.extend(_key_rows(bindings, conflicts, width=width, styles=styles, quit_arm_seconds=quit_arm_seconds))
    rows.extend(["", _heading("Local controls", styles), _dim("/tui <control>", styles)])
    rows.extend(_control_rows(width, styles, capabilities))
    return rows
