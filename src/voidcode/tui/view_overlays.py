"""Interactive selection, confirmation, and approval overlays over typed view data."""

from __future__ import annotations

import json
from collections.abc import Mapping

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from voidcode.sdk.icons import Icon
from voidcode.sdk.json_values import json_wire_value
from voidcode.sdk.views import (
    ChoiceOption,
    ChoicesViewData,
    ConfirmationViewData,
    DiffViewData,
    MarkdownViewData,
    choices_dispatch,
)
from voidcode.tui.gates import ApprovalGate

from .keys import Key
from .styles import Styles, markdown_theme, render_icon, resolve_component, syntax_theme
from .term import clamp_row, escape_terminal_controls, grapheme_spans, render_lines, visible_width
from .view_outcomes import ApprovalDecided, ConfirmationDecided, OverlayActionSelected, OverlayCancelled
from .view_renderers import _styles_arg, render_diff, render_markdown

#: Row step for one overlay page key and the default row budget both scrollable
#: overlay kinds (markdown and artifact) share.
_PAGE_SCROLL_ROWS = 8
OVERLAY_MAX_ROWS = 22


class InteractiveMarkdown:
    """A scrollable detail with explicit actions; never mounted from history replay."""

    def __init__(self, data: MarkdownViewData, *, revision_id: str = "", title: str) -> None:
        self.view = data
        self.revision_id = revision_id
        self.title = title
        self.selected_index = 0
        self.scroll = 0
        self._max_scroll = 0

    @property
    def action(self) -> str:
        return self.view.actions[self.selected_index].action

    def handle_key(self, key: Key) -> OverlayCancelled | OverlayActionSelected | None:
        if key.name == "esc":
            return OverlayCancelled()
        if key.name in ("up", "down", "pageUp", "pageDown", "home", "end"):
            delta = {
                "up": -1,
                "down": 1,
                "pageUp": -_PAGE_SCROLL_ROWS,
                "pageDown": _PAGE_SCROLL_ROWS,
                "home": -self._max_scroll,
                "end": self._max_scroll,
            }[key.name]
            self.scroll = min(self._max_scroll, max(0, self.scroll + delta))
        elif self.view.actions and key.name in ("tab", "right", "shift+tab", "left"):
            self.selected_index = (self.selected_index + (-1 if key.name in ("shift+tab", "left") else 1)) % len(self.view.actions)
        elif self.view.actions and key.name == "enter":
            action = self.view.actions[self.selected_index]
            return OverlayActionSelected(action=action.action, arguments=action.arguments, prepare_input=action.prepare_input)
        return None

    def render_rows(self, width: int, styles: Styles | None = None, *, max_rows: int = OVERLAY_MAX_ROWS) -> list[str]:
        resolved = _styles_arg(styles)
        body = render_markdown(self.view, max(1, width - 4), resolved)
        action_rows = min(3, max(1, max_rows - 5))
        body_rows = max(1, max_rows - action_rows - 4)
        self._max_scroll = max(0, len(body) - body_rows)
        self.scroll = min(self.scroll, self._max_scroll)
        text = Text(clamp_row("↑/↓ scroll · Tab action · Enter · Esc", max(1, width - 4)) + "\n", style=resolved.style("text"))
        text.append_text(Text.from_ansi("\n".join(body[self.scroll : self.scroll + body_rows])))
        indicator = f"Lines {self.scroll + 1}–{min(self.scroll + body_rows, len(body))}/{len(body)}"
        text.append("\n" + clamp_row(indicator, max(1, width - 4)), style=resolved.style("text"))
        start = max(0, self.selected_index - action_rows + 1)
        for index, action in enumerate(self.view.actions[start : start + action_rows], start=start):
            selected = index == self.selected_index
            text.append(
                "\n" + clamp_row(f"{'❯' if selected else ' '} {escape_terminal_controls(action.label)}", max(1, width - 4)),
                style=resolved.style("accent" if selected else "text", bold=selected),
            )
        return render_lines(
            Panel(text, title=escape_terminal_controls(self.title), expand=True), width, theme=markdown_theme(), color_system=resolved.color_system
        )


class InteractiveChoices:
    """Interactive selector with type-to-filter and a selected-visible viewport."""

    def __init__(self, data: ChoicesViewData, *, revision_id: str = "", title: str = "Choose an option") -> None:
        self.title = title
        self.view = data
        #: The revision of the envelope that rendered these options; the
        #: selection is dispatched under exactly that generation.
        self.revision_id = revision_id
        #: Substring filter; printable keys append, Backspace edits, Esc clears first.
        self.filter = ""
        self.selected_index = 0
        self._clamp_selection()

    @property
    def action(self) -> str:
        return self.view.action

    @property
    def argument_name(self) -> str:
        return self.view.argument_name

    @property
    def filtered_options(self) -> tuple[ChoiceOption, ...]:
        """Options matching the filter (label or id, Unicode casefold)."""
        if not self.filter:
            return self.view.options
        needle = self.filter.casefold()
        return tuple(opt for opt in self.view.options if needle in opt.label.casefold() or needle in opt.id.casefold())

    @property
    def selected_id(self) -> str | None:
        filtered = self.filtered_options
        if filtered and 0 <= self.selected_index < len(filtered):
            return filtered[self.selected_index].id
        return None

    def _clamp_selection(self) -> None:
        filtered = self.filtered_options
        if not filtered:
            self.selected_index = 0
        else:
            self.selected_index = min(max(0, self.selected_index), len(filtered) - 1)

    def _restore_selection(self, option_id: str | None) -> None:
        """Keep the stable ID visible after a filter/refresh change, else clamp."""
        if option_id is not None:
            for index, opt in enumerate(self.filtered_options):
                if opt.id == option_id:
                    self.selected_index = index
                    return
        self.selected_index = 0
        self._clamp_selection()

    def update_view(self, data: ChoicesViewData) -> None:
        """Swap options in place: filter kept, stable ID kept when still present."""
        option_id = self.selected_id
        self.view = data
        self._restore_selection(option_id)

    def handle_key(self, key: Key) -> OverlayCancelled | OverlayActionSelected | None:
        if key.name == "esc":
            if self.filter:
                self.filter = ""
                self.selected_index = 0
                return None
            return OverlayCancelled()
        filtered = self.filtered_options
        if key.name == "up":
            if filtered:
                self.selected_index = (self.selected_index - 1) % len(filtered)
            return None
        if key.name == "down":
            if filtered:
                self.selected_index = (self.selected_index + 1) % len(filtered)
            return None
        if key.name in ("pageUp", "pageDown", "home", "end"):
            if filtered:
                if key.name == "home":
                    self.selected_index = 0
                elif key.name == "end":
                    self.selected_index = len(filtered) - 1
                else:
                    delta = -_PAGE_SCROLL_ROWS if key.name == "pageUp" else _PAGE_SCROLL_ROWS
                    self.selected_index = min(max(0, self.selected_index + delta), len(filtered) - 1)
            return None
        if key.name in ("ctrl+u",):
            if self.filter:
                self.filter = ""
                self.selected_index = 0
            return None
        if key.name == "backspace":
            if self.filter:
                option_id = self.selected_id
                spans = grapheme_spans(self.filter)
                self.filter = self.filter[: spans[-1][0]] if spans else ""
                self._restore_selection(option_id)
            return None
        if key.name == "enter":
            if not filtered:
                return None
            action, arguments = choices_dispatch(self.view, filtered[self.selected_index].id)
            return OverlayActionSelected(action=action, arguments=arguments, prepare_input=self.view.prepare_input)
        if key.text and not key.name.startswith("ctrl+") and not key.name.startswith("alt+"):
            option_id = self.selected_id
            self.filter += key.text
            self._restore_selection(option_id)
            return None
        return None

    def render_rows(self, width: int, styles: Styles | None = None, *, max_rows: int | None = None) -> list[str]:
        resolved = _styles_arg(styles)
        fg, bg, tone_slot = resolve_component(self.view.style)
        filtered = self.filtered_options
        self._clamp_selection()
        total = len(self.view.options)
        inner = max(8, width - 4)
        # Floor chrome: panel borders + heading + hint/filter. Overflow markers
        # and the empty row ride inside the option budget so a viewport of N
        # physical rows always keeps the selected option on screen.
        chrome = 2 + 1 + 1

        def row(label: str, index: int, *, selected: bool) -> Text:
            gutter = " > " if selected else "   "
            number = f"[{index + 1}] "
            room = max(1, inner - visible_width(gutter) - visible_width(number))
            clean = escape_terminal_controls(label).replace("\n", " ")
            if visible_width(clean) > room:
                clean = clamp_row(clean, max(1, room - 1)) + "…"
            line = Text(gutter + number, style=resolved.style("warning" if selected else "dim", bold=selected))
            line.append(
                clean,
                style=resolved.style(
                    "warning" if selected else (tone_slot or "text"),
                    color=None if selected else fg,
                    background=None if selected else bg,
                    bold=selected,
                ),
            )
            return line

        if max_rows is None:
            start, end = 0, len(filtered)
        else:
            budget = max(3, max_rows - chrome)
            visible = budget
            if len(filtered) > budget:
                visible = budget - 2  # both overflow markers
            start = max(0, min(self.selected_index, max(0, len(filtered) - max(1, visible))))
            end = min(len(filtered), start + max(1, visible))
            # One-sided overflow gives its unused marker row back to options.
            if len(filtered) > budget and (start == 0 or end == len(filtered)) and end - start < budget - 1:
                if start == 0:
                    end = min(len(filtered), start + budget - 1)
                else:
                    start = max(0, end - (budget - 1))
        hidden_above = start
        hidden_below = len(filtered) - end
        text = Text()
        text.append(f"Choose an option ({len(filtered)}/{total}):\n", style=resolved.style(tone_slot or "accent", color=fg, background=bg, bold=True))
        if hidden_above:
            text.append(Text(f"  ↑ {hidden_above} more\n", style=resolved.style("dim")))
        for i in range(start, end):
            text.append(row(filtered[i].label, i, selected=i == self.selected_index))
            text.append("\n")
        if hidden_below:
            text.append(Text(f"  ↓ {hidden_below} more\n", style=resolved.style("dim")))
        if not filtered:
            if self.view.options:
                text.append(f"  No matches for {escape_terminal_controls(self.filter)!r}.\n", style=resolved.style("dim"))
            else:
                text.append("  No options.\n", style=resolved.style("dim"))
        if self.filter:
            text.append(f"Filter: {escape_terminal_controls(self.filter)}\n", style=resolved.style("dim"))
        else:
            text.append("Type to filter · ↑/↓ select · Enter · Esc\n", style=resolved.style("dim"))
        panel = Panel(
            text,
            title=escape_terminal_controls(self.title),
            border_style=resolved.style(tone_slot or "warning", color=fg, background=bg),
            expand=True,
        )
        rows = render_lines(panel, width, theme=markdown_theme(), color_system=resolved.color_system)
        if max_rows is not None and len(rows) > max_rows:
            rows = rows[: max_rows - 1] + [rows[-1]]
        return rows


class InteractiveConfirmation:
    """Interactive confirmation dialog binding request_id."""

    def __init__(self, data: ConfirmationViewData, *, revision_id: str = "") -> None:
        self.view = data
        #: The revision this confirmation was displayed under; the stored
        #: request id stays the identity, this is the generation the user saw.
        self.revision_id = revision_id
        self.selected_index = 0

    @property
    def request_id(self) -> str:
        return self.view.request_id

    @property
    def message(self) -> str:
        return self.view.message

    @property
    def actions(self) -> tuple[str, ...]:
        return self.view.actions

    def handle_key(self, key: Key) -> ConfirmationDecided | None:
        if key.name in ("left", "h", "up", "k"):
            self.selected_index = (self.selected_index - 1) % len(self.view.actions)
            return None
        if key.name in ("right", "l", "down", "j", "tab"):
            self.selected_index = (self.selected_index + 1) % len(self.view.actions)
            return None
        if key.name == "enter":
            return ConfirmationDecided(request_id=self.view.request_id, decision=self.view.actions[self.selected_index])
        if key.name == "esc":
            return ConfirmationDecided(request_id=self.view.request_id, decision="deny")
        return None

    def render_rows(self, width: int, styles: Styles | None = None) -> list[str]:
        resolved = _styles_arg(styles)
        fg, bg, tone_slot = resolve_component(self.view.style)
        text = Text()
        message = escape_terminal_controls(self.view.message)
        text.append(f"{message}\n\n", style=resolved.style(tone_slot or "text", color=fg, background=bg, bold=True))
        text.append("Request ID: ", style=resolved.style("dim"))
        text.append(f"{escape_terminal_controls(self.view.request_id)}\n\n", style=resolved.style(tone_slot or "accent", color=fg, background=bg))
        for i, act in enumerate(self.view.actions):
            clean = escape_terminal_controls(act)
            is_selected = i == self.selected_index
            if is_selected and act == "allow":
                style = resolved.style("success", bold=True, reverse=True)
            elif is_selected:
                style = resolved.style("error", bold=True, reverse=True)
            else:
                style = resolved.style("dim")
            text.append(f" [ {clean.upper()} ] ", style=style)
            text.append("  ")
        panel = Panel(text, title="Action Confirmation", border_style=resolved.style(tone_slot or "error", color=fg, background=bg), expand=True)
        return render_lines(panel, width, theme=markdown_theme(), color_system=resolved.color_system)


class InteractiveApproval:
    """Native tool-approval gate: full arguments plus a diff preview, scrollable.

    The decision outcome: ``{"request_id", "decision", "grant_session_policy", "reason"}``.
    """

    def __init__(self, gate: ApprovalGate, *, icon: Icon | None = None) -> None:
        self.gate = gate
        self.icon = icon
        self.request_id = gate.request_id
        self.tool_name = gate.tool_name
        self.arguments: Mapping[str, object] = gate.call.arguments
        self.scroll = 0
        self.reason_mode: bool = False
        self.reason_input: str = ""

    def handle_key(self, key: Key) -> ApprovalDecided | None:
        if self.reason_mode:
            if key.name in ("esc", "escape"):
                self.reason_mode = False
                self.reason_input = ""
                return None
            if key.name in ("enter", "return"):
                reason = self.reason_input.strip() or "denied by user"
                return ApprovalDecided(request_id=self.request_id, decision="deny", reason=reason)
            if key.name == "backspace":
                self.reason_input = self.reason_input[:-1]
                return None
            if key.text:
                self.reason_input += key.text
                return None
            return None

        if key.name in ("up", "k", "pageUp"):
            self.scroll = max(0, self.scroll - 1)
            return None
        if key.name in ("down", "j", "pageDown", "space"):
            self.scroll += 1
            return None
        if key.name == "s":
            return ApprovalDecided(request_id=self.request_id, decision="allow", grant_session_policy=True)
        if key.name == "r":
            self.reason_mode = True
            self.reason_input = ""
            return None
        if key.name in ("a", "enter"):
            return ApprovalDecided(request_id=self.request_id, decision="allow")
        if key.name in ("d", "esc"):
            return ApprovalDecided(request_id=self.request_id, decision="deny")
        return None

    def _edit_preview(self) -> tuple[str, str, str] | None:
        """(path, before, after) for a file edit, else None."""
        if self.tool_name != "edit":
            return None
        path = self.arguments.get("path")
        if not isinstance(path, str) or not path:
            return None
        edits = self.arguments.get("edits")
        if isinstance(edits, (list, tuple)) and edits:
            before_parts: list[str] = []
            after_parts: list[str] = []
            for edit in edits:
                if not isinstance(edit, Mapping):
                    return None
                old, new = edit.get("oldString"), edit.get("newString")
                if not isinstance(old, str) or not isinstance(new, str):
                    return None
                before_parts.append(old)
                after_parts.append(new)
            return path, "\n".join(before_parts), "\n".join(after_parts)
        old, new = self.arguments.get("oldString"), self.arguments.get("newString")
        if isinstance(old, str) and isinstance(new, str):
            return path, old, new
        return None

    def render_rows(self, width: int, styles: Styles | None = None) -> list[str]:
        resolved = _styles_arg(styles)
        glyph = render_icon(self.icon)
        header = Text.assemble(
            ((f"{glyph} " if glyph else ""), resolved.style("warning")),
            ("Tool approval required", resolved.style("warning", bold=True)),
        )
        rows = render_lines(header, width, theme=markdown_theme(), color_system=resolved.color_system)
        rows.append(resolved.fg("text", f"  tool: {escape_terminal_controls(self.tool_name)}"))
        rows.append(resolved.fg("dim", f"  request: {escape_terminal_controls(self.request_id)}"))
        preview = self._edit_preview()
        if preview is not None:
            path, before, after = preview
            rows.extend(render_diff(DiffViewData(before=before, after=after, path=path), width, resolved))
        pretty = json.dumps(json_wire_value(self.arguments), indent=2, ensure_ascii=False, default=str)
        if pretty and pretty != "{}":
            rows.append(resolved.fg("dim", "  arguments:"))
            rows.extend(
                render_lines(
                    Syntax(
                        escape_terminal_controls(pretty, preserve_newlines=True, preserve_tabs=True),
                        "json",
                        theme=syntax_theme(),
                        word_wrap=True,
                    ),
                    width,
                    theme=markdown_theme(),
                    color_system=resolved.color_system,
                )
            )
        visible = rows[self.scroll :] if self.scroll < len(rows) else []
        if self.reason_mode:
            visible.append(resolved.fg("warning", f"  Denial reason: {escape_terminal_controls(self.reason_input)}█"))
            visible.append(resolved.fg("dim", "  [Enter] submit denial with reason    [Esc] cancel"))
        else:
            visible.append(resolved.fg("text", "  [a] allow once    [s] allow for session    [d] deny    [r] deny with reason    [up/down] scroll"))
        return visible
