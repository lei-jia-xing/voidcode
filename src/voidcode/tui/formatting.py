"""Shared tone, prefix, padding, and receipt formatting for the TUI.

One implementation per rule: notices, run status, command receipts, and the
footer all use the same tone vocabulary; width math delegates to ``term``.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from typing import Literal

from voidcode.sdk.client import CommandResult
from voidcode.sdk.command_help import public_command_path
from voidcode.sdk.commands import CommandDescriptor
from voidcode.sdk.views import PluginViewDescriptor

from .styles import Styles
from .term import clamp_row, crop_row_right, escape_terminal_controls, visible_width

type NoticeTone = Literal["info", "success", "warning", "error"]


class StatusLineKind(StrEnum):
    """Why the status line is showing; branch on this, never on its text."""

    NOTICE = "notice"
    LOADING = "loading"
    FOLLOWUP_QUEUED = "followup_queued"


TONE_PREFIX: dict[NoticeTone | str, str] = {"warning": "Warning: ", "error": "Error: "}
TONE_SLOT: dict[NoticeTone | str, str] = {
    "info": "accent",
    "success": "success",
    "ok": "success",
    "warning": "warning",
    "error": "error",
}

#: App/editor keys a catalog shortcut may never claim; the client keeps them.
#: Compared case-insensitively: catalog chords are lowercase (``alt+pageup``)
#: while the decoder names keys like ``pageUp``.
RESERVED_SHORTCUTS: frozenset[str] = frozenset(
    {
        "escape",
        "esc",
        "enter",
        "tab",
        "shift+tab",
        "space",
        "backspace",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "ctrl+o",
        "ctrl+c",
        "ctrl+d",
        "ctrl+s",
        "ctrl+enter",
        "shift+enter",
        "ctrl+j",
        "alt+enter",
        "alt+left",
        "alt+right",
        "alt+b",
        "alt+f",
        "ctrl+left",
        "ctrl+right",
        "ctrl+w",
        "alt+backspace",
        "ctrl+backspace",
        "alt+delete",
        "alt+d",
        "alt+e",
        "alt+shift+e",
        "alt+c",
        "alt+shift+c",
        "alt+x",
        "alt+shift+x",
        "alt+up",
        "alt+i",
        "alt+v",
        "alt+r",
    }
)


def safe(value: object) -> str:
    """One-line, control-free rendering of any plugin- or model-provided value."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return escape_terminal_controls(text, preserve_tabs=False)


def json_safe(value: object) -> object:
    """Round-trip unknown payload pieces through JSON so anything is serializable."""
    from voidcode.sdk.json_values import json_wire_value

    try:
        return json.loads(json.dumps(json_wire_value(value), ensure_ascii=False, allow_nan=False, default=str))
    except TypeError, ValueError:
        return str(value)


def tone_prefix(tone: NoticeTone | str) -> str:
    """Canonical visible prefix for a tone; ordinary info has none."""
    return TONE_PREFIX.get(tone, "")


def tone_slot(tone: NoticeTone | str) -> str:
    """Canonical style slot for a tone (unknown tones read as ordinary text)."""
    return TONE_SLOT.get(tone, "accent")


def notice_row(text: object, tone: NoticeTone | str, styles: Styles) -> str:
    """One status/notice row with the shared prefix and slot."""
    return styles.fg(tone_slot(tone), f"{tone_prefix(tone)}{safe(text)}")


def prefixed_rows(prefix: str, text: str, styles: Styles, *, slot: str) -> list[str]:
    """Prefix sanitized multiline text, one row per explicit line.

    Terminal owns physical width wrapping, so this helper never clips content.
    """
    label = f"{styles.render(slot, prefix, bold=True)} "
    clean = escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True)
    rows: list[str] = []
    for index, line in enumerate(clean.split("\n")):
        head = label if index == 0 else " " * len(prefix) + " "
        rows.append(f"{head}{line}")
    return rows or [label]


def pad_visible_row(text: str, width: int) -> str:
    """Pad an ANSI-styled row to ``width`` visible cells (never shrink it)."""
    return text + " " * max(0, width - visible_width(text))


def slash_public_path(descriptor: CommandDescriptor) -> str:
    """One public slash path per catalog entry, derived by the SDK projectors."""
    return public_command_path(descriptor)


def slash_display(name: str) -> str:
    """Human slash spelling for a machine command id (result rows, notices, status)."""
    return name.replace(".", " ")


def command_result_text(result: CommandResult) -> str:
    """One command result's human text: error, then output, then pretty data.

    ``CommandResult`` carries both human text (``output``) and machine data
    (``data``). The TUI renders the human text when present and falls back to
    JSON for data-only receipts (for example the core ``show`` command). The
    fallback preserves an empty mapping and nested falsy values because it
    serializes the value itself, never a truthiness test.
    """
    if result.status == "error" and isinstance(result.error, str) and result.error:
        return result.error
    if isinstance(result.output, str) and result.output:
        return result.output
    if result.data is not None:
        return json.dumps(json_safe(result.data), indent=2, ensure_ascii=False)
    return ""


def command_result_rows(name: str, result: CommandResult, styles: Styles, width: int = 80) -> list[str]:
    """Render direct receipts and durable command results consistently."""
    from .views import render_plugin_view

    label = safe(slash_display(name) or "command")
    text = command_result_text(result)
    if result.status == "error":
        return prefixed_rows(f"Error: /{label}:", text or "Command failed", styles, slot="error")
    if result.status == "pending_confirmation":
        return [styles.fg("warning", f"Confirmation required: /{label}")]
    if result.status == "ok":
        presentation = result.presentation
        if presentation is not None and presentation.view_type not in ("choices", "form"):
            return render_plugin_view(
                PluginViewDescriptor(owner="command", name=name, title=presentation.title, view_type=presentation.view_type, data=presentation.data),
                width,
                styles,
            )
        return escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True).split("\n") if text else []
    return prefixed_rows(f"Warning: /{label} ({result.status or 'unknown'}):", text, styles, slot="warning")


def normalize_chord(name: str) -> str:
    """Case-insensitive chord form so ``alt+pageUp`` matches ``alt+pageup``."""
    return name.strip().lower()


def shortcut_bindings(descriptors: tuple[CommandDescriptor, ...]) -> tuple[dict[str, CommandDescriptor], dict[str, tuple[str, ...]]]:
    """Map one chord to one descriptor; a chord with 2+ claimants binds nobody.

    Reserved app/editor keys never bind. Returns ``(bindings, conflicts)`` where
    conflicts names every claimant so the client can say so explicitly.
    """
    claimants: dict[str, list[CommandDescriptor]] = {}
    for descriptor in descriptors:
        chord = descriptor.default_shortcut
        if not chord:
            continue
        normalized = normalize_chord(chord)
        if normalized in RESERVED_SHORTCUTS:
            continue
        claimants.setdefault(normalized, []).append(descriptor)
    bindings: dict[str, CommandDescriptor] = {}
    conflicts: dict[str, tuple[str, ...]] = {}
    for chord, entries in claimants.items():
        if len(entries) == 1:
            bindings[chord] = entries[0]
        else:
            conflicts[chord] = tuple(entry.name for entry in entries)
    return bindings, conflicts


def model_segment(model: object) -> str:
    """`provider/model`, elif raw, else `no model` — from the frozen health.model DTO only."""
    if model is None:
        return "no model"
    raw = getattr(model, "raw", None)
    provider = getattr(model, "provider", None)
    name = getattr(model, "model", None)
    if isinstance(provider, str) and provider and isinstance(name, str) and name:
        return f"{provider}/{name}"
    if isinstance(raw, str) and raw:
        return raw
    return "no model"


def effort_segment(model: object) -> str | None:
    """reasoning_effort verbatim; None hides the segment (no marker)."""
    effort = getattr(model, "reasoning_effort", None)
    return effort if isinstance(effort, str) and effort else None


def levels_text(model: object) -> str:
    """Supported levels verbatim, generic over the DTO; empty/unknown stays honest."""
    levels = getattr(model, "reasoning_levels", None)
    if isinstance(levels, (list, tuple)) and levels and all(isinstance(entry, str) for entry in levels):
        return ", ".join(levels)
    return "unknown"


def short_workspace(path: str, *, local: bool) -> str:
    """`~`-shorten only when the path is known local; server paths stay full (crop handles width)."""
    if not local:
        return path
    home = os.path.expanduser("~")
    if home and home != "~" and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home) :]
    return path


def footer_row(
    workspace: str,
    model: str,
    effort: str | None,
    context: str | None = None,
    usage: str | None = None,
    *,
    width: int,
    compact: bool,
) -> str:
    """Context row with width degrade: drop usage → crop workspace → drop workspace → drop context → model."""
    sep = " · "
    if compact:
        return clamp_row(sep.join([s for s in (model, effort) if s]), width)

    # 1. Full: workspace · model · effort · context · usage
    full_segments = [s for s in (workspace, model, effort, context, usage) if s]
    text = sep.join(full_segments)
    if visible_width(text) <= width:
        return text

    # 2. If too wide, drop usage: workspace · model · effort · context
    if usage:
        segments = [s for s in (workspace, model, effort, context) if s]
        text = sep.join(segments)
        if visible_width(text) <= width:
            return text

    # 3. If too wide, crop workspace or drop workspace: model · effort · context
    tail_with_ctx = [s for s in (model, effort, context) if s]
    rest = sep.join(tail_with_ctx)
    if workspace:
        rest_w = visible_width(sep + rest) if rest else 0
        room = max(0, width - rest_w)
        if room >= 8 and visible_width(workspace) > room:
            return crop_row_right(workspace, room) + (sep + rest if rest else "")

    text = sep.join(tail_with_ctx)
    if visible_width(text) <= width:
        return text

    # 4. If too wide, drop context: model · effort
    tail = [s for s in (model, effort) if s]
    text = sep.join(tail)
    if visible_width(text) <= width:
        return text

    # 5. If too wide (40 cols), model (clamped)
    return clamp_row(model, width)
