"""Pure renderers and interactive models for the 11 finite core plugin UI kinds."""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from voidcode.sdk.gates import ApprovalGate
from voidcode.sdk.icons import Icon
from voidcode.sdk.json_values import json_wire_value
from voidcode.sdk.views import (
    ChoiceOption,
    ChoicesViewData,
    ConfirmationViewData,
    DiffViewData,
    FieldsViewData,
    FormViewData,
    JsonViewData,
    MarkdownViewData,
    MediaViewData,
    NoticeViewData,
    PluginViewDescriptor,
    StatusViewData,
    TableViewData,
    choices_dispatch,
    parse_view_data,
    validate_form_submission,
)

from .keys import Key
from .styles import Styles, markdown_theme, render_icon, resolve_component, syntax_theme
from .term import clamp_row, escape_terminal_controls, grapheme_spans, render_lines, visible_width


def _styles_arg(styles: Styles | None) -> Styles:
    """The caller's styles, or a default-depth handle when none travels with the call."""
    return styles if styles is not None else Styles()


class UnsupportedViewError(ValueError):
    """Raised when a plugin attempts to render an unrecognised view type."""


def render_notice(data: NoticeViewData, width: int, styles: Styles | None = None) -> list[str]:
    resolved = _styles_arg(styles)
    fg, bg, tone_slot = resolve_component(data.style, level=data.level)
    prefix = {"warning": "Warning: ", "error": "Error: "}.get(data.level, "")
    slot = tone_slot or {"info": "accent", "warning": "warning", "error": "error"}.get(data.level, "accent")
    message_slot = "text" if prefix or slot not in ("warning", "error", "success") else slot
    content = Text.assemble(
        (prefix, resolved.style(slot, color=fg, background=bg, bold=True)),
        (escape_terminal_controls(data.message), resolved.style(message_slot, color=fg, background=bg)),
    )
    return render_lines(content, width, theme=markdown_theme(), color_system=resolved.color_system)


def render_assistant_markdown(text: str, width: int, styles: Styles | None = None) -> list[str]:
    """Assistant prose through the same markdown path as plugin views.

    One shared renderer for both so assistant output and plugin markdown can
    never drift apart in styling or control-byte handling.
    """
    return render_markdown(MarkdownViewData(content=text), width, styles)


def render_user_message(text: str, width: int, styles: Styles | None = None) -> list[str]:
    """User text verbatim (newlines/code/HTML-like syntax intact); no role label."""
    resolved = _styles_arg(styles)
    clean = escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True)
    return render_lines(
        Text(clean, style=resolved.style("text", bold=True)),
        width,
        theme=markdown_theme(),
        color_system=resolved.color_system,
    )


def render_thinking(text: str, width: int, styles: Styles | None = None) -> list[str]:
    """Reasoning trace as muted italic text; no label, no toggle."""
    resolved = _styles_arg(styles)
    clean = escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True)
    return render_lines(
        Text(clean, style=resolved.style("muted", italic=True)),
        width,
        theme=markdown_theme(),
        color_system=resolved.color_system,
    )


def render_tool_call(name: str, arguments: object, width: int, styles: Styles | None = None, icon: Icon | None = None) -> list[str]:
    """Compact tool intent with a bounded argument preview; history keeps full data."""
    resolved = _styles_arg(styles)
    pretty = json.dumps(json_wire_value(arguments), ensure_ascii=False, default=str)
    glyph = render_icon(icon)
    header = Text.assemble(
        ((f"{glyph} " if glyph else ""), resolved.style("dim")),
        (escape_terminal_controls(name), resolved.style("text", bold=True)),
    )
    rows = render_lines(header, width, theme=markdown_theme(), color_system=resolved.color_system)
    if pretty and pretty != "{}":
        preview = render_lines(
            Text(escape_terminal_controls(pretty), style=resolved.style("dim")),
            width,
            theme=markdown_theme(),
            color_system=resolved.color_system,
        )
        rows.extend(preview[:2])
        if len(preview) > 2:
            rows.append(resolved.fg("dim", '… arguments shortened · /show {"events":true}'))
    return rows


def render_active_tool(
    name: str,
    elapsed_seconds: float,
    output_tail: list[str],
    width: int,
    styles: Styles | None = None,
    icon: Icon | None = None,
    spinner: str = "",
    max_tail_rows: int = 0,
) -> list[str]:
    """Active tool in the Live region: 1-row header + allocated tail display rows."""
    resolved = _styles_arg(styles)
    glyph = render_icon(icon)
    spin = f"{spinner} " if spinner else ""
    elapsed = f" [{elapsed_seconds:.1f}s]"
    header = Text.assemble(
        (spin, resolved.style("accent")),
        ((f"{glyph} " if glyph else ""), resolved.style("dim")),
        (escape_terminal_controls(name), resolved.style("text", bold=True)),
        (elapsed, resolved.style("dim")),
    )
    header_lines = render_lines(header, width, theme=markdown_theme(), color_system=resolved.color_system)
    rows = [header_lines[0]] if header_lines else []

    if max_tail_rows > 0 and output_tail:
        tail_display_rows: list[str] = []
        for line in output_tail:
            styled_line = Text(f"  │ {escape_terminal_controls(line)}", style=resolved.style("dim"))
            wrapped = render_lines(styled_line, width, theme=markdown_theme(), color_system=resolved.color_system)
            tail_display_rows.extend(wrapped)
        if tail_display_rows:
            rows.extend(tail_display_rows[-max_tail_rows:])
    return rows


def render_active_tools_summary(
    tools: list[tuple[str, float]],
    width: int,
    styles: Styles | None = None,
    spinner: str = "",
) -> list[str]:
    """Compact summary when active tools exceed available viewport rows."""
    resolved = _styles_arg(styles)
    spin = f"{spinner} " if spinner else ""
    names = ", ".join(f"{name} [{elapsed:.0f}s]" for name, elapsed in tools)
    text = Text.assemble(
        (spin, resolved.style("accent")),
        (f"running {len(tools)} tools: ", resolved.style("text", bold=True)),
        (escape_terminal_controls(names), resolved.style("dim")),
    )
    lines = render_lines(text, width, theme=markdown_theme(), color_system=resolved.color_system)
    return [lines[0]] if lines else []


def _artifact_ref_uri(value: object) -> str | None:
    """A wire artifact reference's URI, or None when the value is not a typed reference."""
    if not isinstance(value, Mapping):
        return value if isinstance(value, str) and value.startswith("artifact://") else None
    digest = value.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    return f"artifact://{digest}"


def _artifact_ref_bytes(value: object) -> str:
    """A wire artifact reference's byte count, or an explicit unknown marker."""
    if isinstance(value, Mapping):
        total = value.get("bytes")
        if type(total) is int and total >= 0:
            return str(total)
    return "unknown"


def _composite_part_text(part: Mapping[str, Any]) -> str:
    """One composite part's ordered readable text from its wire dict.

    The renderer never raises: unknown kinds and malformed fields render as
    explicit markers naming the problem, so scrollback always stays readable.
    """
    kind = part.get("kind")
    if kind == "text":
        text = part.get("text")
        body = text if isinstance(text, str) else ""
        uri = _artifact_ref_uri(part.get("reference"))
        if uri is not None:
            suffix = f"[full text: {uri}]"
            return f"{body}\n{suffix}" if body else suffix
        return body
    if kind == "media":
        mime = part.get("mime")
        mime_text = mime if isinstance(mime, str) and mime else "unknown"
        alt = part.get("presentation")
        alt_text = alt if isinstance(alt, str) and alt else "(missing presentation)"
        reference = part.get("reference")
        uri = _artifact_ref_uri(reference)
        label = "Image" if mime_text.startswith("image/") else "Audio" if mime_text.startswith("audio/") else "Media"
        return f"[{label}: {mime_text}, {_artifact_ref_bytes(reference)} bytes, {uri if uri is not None else 'missing-uri'}] {alt_text}"
    if kind == "table":
        raw_columns = part.get("columns")
        columns = [column for column in raw_columns if isinstance(column, str)] if isinstance(raw_columns, list) else []
        if not columns:
            return "[table: missing columns]"
        raw_rows = part.get("rows")
        lines = ["\t".join(columns)]
        shown = 0
        if isinstance(raw_rows, list):
            for row in raw_rows:
                if not isinstance(row, list):
                    continue
                lines.append("\t".join(cell if isinstance(cell, str) else "" for cell in row))
                shown += 1
        caption = part.get("presentation")
        if isinstance(caption, str) and caption:
            lines.insert(0, caption)
        total = part.get("total_rows")
        if type(total) is int and total > shown:
            uri = _artifact_ref_uri(part.get("reference"))
            note = f"[showing {shown}/{total} rows" + (f"; full table: {uri}]" if uri is not None else "]")
            lines.append(note)
        return "\n".join(lines)
    if kind == "json":
        preview = part.get("preview")
        preview_text = preview if isinstance(preview, str) and preview else "(missing preview)"
        uri = _artifact_ref_uri(part.get("reference"))
        return f"{preview_text}\n[full JSON: {uri if uri is not None else 'missing-uri'}]"
    return f"[unknown part kind: {kind!r}]"


def _composite_part_has_reference(part: object) -> bool:
    return isinstance(part, Mapping) and _artifact_ref_uri(part.get("reference")) is not None


def render_tool_result(
    result: Mapping[str, Any],
    width: int,
    styles: Styles | None = None,
    *,
    interrupted: bool = False,
) -> list[str]:
    """Preserve selected inline output; immutable source paging is a separate action."""
    resolved = _styles_arg(styles)
    failed = interrupted or result.get("status") == "error"
    rows: list[str] = []
    output = result.get("output")
    bodies: list[str] = []
    suffix = ""
    artifact_available = result.get("result_reference") is not None
    if isinstance(output, Mapping):
        output_type = str(output.get("type", ""))
        if output_type == "text":
            presentation = output.get("presentation")
            bodies.append(str(presentation) if isinstance(presentation, str) and presentation else str(output.get("text", "")))
        elif output_type == "attachment":
            bodies.append(f"[{output.get('mime') or 'attachment'}]")
        elif output_type == "empty":
            presentation = output.get("presentation")
            bodies.append(presentation if isinstance(presentation, str) else "")
        elif output_type == "composite":
            parts = output.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if isinstance(part, Mapping):
                        bodies.append(_composite_part_text(part))
                        artifact_available = artifact_available or _composite_part_has_reference(part)
        bounds = output.get("bounds")
        if isinstance(bounds, Mapping):
            flags = [key for key in ("truncated", "partial") if bounds.get(key) is True]
            artifact_available = artifact_available or bounds.get("reference") is not None
            source = bounds.get("source")
            note = ", ".join([*flags, str(source)] if isinstance(source, str) and source else flags)
            if note:
                suffix = f"[{note}]"
    body = "\n\n".join(block for block in bodies if block)
    if failed:
        if body and isinstance(output, Mapping) and str(output.get("type", "")) == "composite":
            rows.extend(
                render_lines(
                    Text(escape_terminal_controls(body, preserve_newlines=True, preserve_tabs=True), style=resolved.style("dim")),
                    width,
                    theme=markdown_theme(),
                    color_system=resolved.color_system,
                )
            )
        error = result.get("error")
        label = "Interrupted" if interrupted else "Error"
        detail = escape_terminal_controls(str(error or body or "interrupted"), preserve_newlines=True)
        rows.extend(
            render_lines(
                Text(f"{label}: {detail}", style=resolved.style("dim" if interrupted else "error")),
                width,
                theme=markdown_theme(),
                color_system=resolved.color_system,
            )
        )
    elif body:
        preview = render_lines(
            Text(escape_terminal_controls(body, preserve_newlines=True, preserve_tabs=True), style=resolved.style("dim")),
            width,
            theme=markdown_theme(),
            color_system=resolved.color_system,
        )
        rows.extend(preview)
    if artifact_available:
        rows.append(resolved.fg("dim", "Artifacts: Ctrl+O / /tui output · original sources and structured result are separate"))
    if suffix:
        rows.append(resolved.fg("dim", suffix))
    return rows


def render_markdown(data: MarkdownViewData, width: int, styles: Styles | None = None) -> list[str]:
    """Render plugin markdown.

    Control bytes are escaped first, so plugin text cannot emit terminal
    commands; rich's Markdown then owns headings, lists, tables and code.
    """
    resolved = _styles_arg(styles)
    content = escape_terminal_controls(data.content, preserve_newlines=True, preserve_tabs=True)
    document = Markdown(content)
    # rich names ``code_theme`` str but hands it straight to ``Syntax(theme=...)``,
    # which takes a SyntaxTheme: set it post-construction instead of lying in the call.
    document.__dict__["code_theme"] = syntax_theme()
    return render_lines(document, width, theme=markdown_theme(), color_system=resolved.color_system)


def render_fields(data: FieldsViewData, width: int, styles: Styles | None = None) -> list[str]:
    resolved = _styles_arg(styles)
    fg, bg, tone_slot = resolve_component(data.style)
    table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    table.add_column("Key", style=resolved.style(tone_slot or "link", color=fg, background=bg, bold=True), no_wrap=True)
    table.add_column("Value", style=resolved.style(tone_slot or "text", color=fg, background=bg), overflow="fold")
    for row in data.rows:
        table.add_row(
            Text(escape_terminal_controls(row.label)),
            Text(escape_terminal_controls(row.value)),
        )
    return render_lines(table, width, theme=markdown_theme(), color_system=resolved.color_system)


def render_diff(data: DiffViewData, width: int, styles: Styles | None = None) -> list[str]:
    resolved = _styles_arg(styles)
    fg, bg, tone_slot = resolve_component(data.style)
    diff_lines = list(
        difflib.unified_diff(
            data.before.splitlines(),
            data.after.splitlines(),
            fromfile=f"a/{data.path or 'before'}",
            tofile=f"b/{data.path or 'after'}",
            lineterm="",
        )
    )
    diff_text = Text()
    heading = f"Diff: {escape_terminal_controls(str(data.path))}" if data.path else "Diff"
    diff_text.append(heading, style=resolved.style(tone_slot or "text", color=fg, background=bg, bold=True))
    for line in diff_lines:
        diff_text.append("\n")
        clean = escape_terminal_controls(line)
        if line.startswith("+") and not line.startswith("+++"):
            diff_text.append(clean, style=resolved.style("diff_added"))
        elif line.startswith("-") and not line.startswith("---"):
            diff_text.append(clean, style=resolved.style("diff_removed"))
        elif line.startswith("@@"):
            diff_text.append(clean, style=resolved.style("accent"))
        else:
            diff_text.append(clean, style=resolved.style(tone_slot or "diff_context", color=fg, background=bg))
    return render_lines(diff_text, width, theme=markdown_theme(), color_system=resolved.color_system)


def render_status(data: StatusViewData, width: int, styles: Styles | None = None) -> list[str]:
    resolved = _styles_arg(styles)
    fg, bg, tone_slot = resolve_component(data.style)
    text = Text.assemble(
        ("* ", resolved.style(tone_slot or "success", color=fg, background=bg, bold=True)),
        (f"{escape_terminal_controls(data.label)}: ", resolved.style(tone_slot or "text", color=fg, background=bg, bold=True)),
        (escape_terminal_controls(data.value), resolved.style(tone_slot or "text", color=fg, background=bg)),
    )
    return render_lines(text, width, theme=markdown_theme(), color_system=resolved.color_system)


def render_table(data: TableViewData, width: int, styles: Styles | None = None) -> list[str]:
    resolved = _styles_arg(styles)
    fg, bg, tone_slot = resolve_component(data.style)
    table = Table(show_header=True, box=None, padding=(0, 2), expand=False)
    for column in data.columns:
        table.add_column(
            Text(escape_terminal_controls(column)),
            style=resolved.style(tone_slot or "link", color=fg, background=bg, bold=True),
            overflow="fold",
        )
    value_style = resolved.style(tone_slot or "text", color=fg, background=bg)
    for row in data.rows:
        table.add_row(*[Text(escape_terminal_controls(cell), style=value_style) for cell in row])
    rows = render_lines(table, width, theme=markdown_theme(), color_system=resolved.color_system)
    if data.total_rows is not None and data.total_rows > len(data.rows) and data.reference is not None:
        rows.append(resolved.fg("dim", f"[showing {len(data.rows)}/{data.total_rows} rows; full table: {data.reference.uri}]"))
    return rows


def render_json(data: JsonViewData, width: int, styles: Styles | None = None) -> list[str]:
    resolved = _styles_arg(styles)
    fg, bg, tone_slot = resolve_component(data.style)
    preview = Text(
        escape_terminal_controls(data.preview, preserve_newlines=True, preserve_tabs=True),
        style=resolved.style(tone_slot or "text", color=fg, background=bg),
    )
    rows = render_lines(preview, width, theme=markdown_theme(), color_system=resolved.color_system)
    if data.reference is not None:
        rows.append(resolved.fg("dim", f"[full JSON: {data.reference.uri}]"))
    return rows


def render_media(data: MediaViewData, width: int, styles: Styles | None = None) -> list[str]:
    resolved = _styles_arg(styles)
    fg, bg, tone_slot = resolve_component(data.style)
    label = "Image" if data.mime.startswith("image/") else "Audio" if data.mime.startswith("audio/") else "Media"
    line = f"[{label}: {data.mime}, {data.reference.bytes} bytes, {data.reference.uri}] {escape_terminal_controls(data.alt)}"
    return render_lines(
        Text(line, style=resolved.style(tone_slot or "text", color=fg, background=bg)),
        width,
        theme=markdown_theme(),
        color_system=resolved.color_system,
    )


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

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name == "esc":
            return {"cancel": True}
        if key.name in ("up", "down", "pageUp", "pageDown", "home", "end"):
            delta = {"up": -1, "down": 1, "pageUp": -8, "pageDown": 8, "home": -self._max_scroll, "end": self._max_scroll}[key.name]
            self.scroll = min(self._max_scroll, max(0, self.scroll + delta))
        elif self.view.actions and key.name in ("tab", "right", "shift+tab", "left"):
            self.selected_index = (self.selected_index + (-1 if key.name in ("shift+tab", "left") else 1)) % len(self.view.actions)
        elif self.view.actions and key.name == "enter":
            action = self.view.actions[self.selected_index]
            return {"action": action.action, "arguments": dict(action.arguments), "prepare_input": action.prepare_input}
        return None

    def render_rows(self, width: int, styles: Styles | None = None, *, max_rows: int = 22) -> list[str]:
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

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name == "esc":
            if self.filter:
                self.filter = ""
                self.selected_index = 0
                return None
            return {"cancel": True}
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
                    delta = -8 if key.name == "pageUp" else 8
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
            return {"action": action, "arguments": arguments, "prepare_input": self.view.prepare_input}
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

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name in ("left", "h", "up", "k"):
            self.selected_index = (self.selected_index - 1) % len(self.view.actions)
            return None
        if key.name in ("right", "l", "down", "j", "tab"):
            self.selected_index = (self.selected_index + 1) % len(self.view.actions)
            return None
        if key.name == "enter":
            return {"request_id": self.view.request_id, "decision": self.view.actions[self.selected_index]}
        if key.name == "esc":
            return {"request_id": self.view.request_id, "decision": "deny"}
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

    The decision is the only outcome: ``{"request_id", "decision"}`` on allow,
    deny, or esc (deny). Scroll keys move the preview; they never decide.
    """

    def __init__(self, gate: ApprovalGate, *, icon: Icon | None = None) -> None:
        self.gate = gate
        self.icon = icon
        self.request_id = gate.request_id
        self.tool_name = gate.tool_name
        self.arguments: Mapping[str, Any] = gate.call.arguments
        self.scroll = 0

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if key.name in ("up", "k", "pageUp"):
            self.scroll = max(0, self.scroll - 1)
            return None
        if key.name in ("down", "j", "pageDown", "space"):
            self.scroll += 1
            return None
        if key.name in ("a", "enter"):
            return {"request_id": self.request_id, "decision": "allow"}
        if key.name in ("d", "esc"):
            return {"request_id": self.request_id, "decision": "deny"}
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
        visible.append(resolved.fg("text", "  [a] allow once    [d] deny    [up/down] scroll"))
        return visible


class InteractiveForm:
    """Interactive Form View supporting text, multiline, integer, number, boolean."""

    def __init__(
        self, data: FormViewData, *, action_schema: Mapping[str, Any] | None = None, revision_id: str = "", title: str = "Command input"
    ) -> None:
        self.title = title
        self.view = data
        #: The revision of the views envelope that rendered this form: the
        #: read prefill and the submit both travel under it.
        self.revision_id = revision_id
        self.load_error: str | None = None
        self._action_schema = action_schema
        self._field_names = frozenset(field.name for field in data.fields)

        self.values: dict[str, Any] = {}
        if isinstance(data.initial_values, Mapping):
            for key, value in data.initial_values.items():
                if key in self._field_names:
                    self.values[key] = value

        # No field is materialised: a missing optional value stays missing so it
        # can be omitted, cleared (null) or explicitly set at submit time.

        self.active_index = 0
        self.dirty_fields: set[str] = set()
        self.validation_error: str | None = None

    @property
    def action(self) -> str:
        return self.view.action

    @property
    def load_action(self) -> str | None:
        return self.view.load_action

    def populate_from_load_action(self, loaded_data: Mapping[str, Any]) -> None:
        """Prefill from load_action; fields the user already edited keep their draft."""
        for key, value in loaded_data.items():
            if key in self._field_names and key not in self.dirty_fields:
                self.values[key] = value

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        self.validation_error = None
        if key.name in ("tab", "down"):
            if self.view.fields:
                self.active_index = (self.active_index + 1) % len(self.view.fields)
            return None
        if key.name in ("shift+tab", "up"):
            if self.view.fields:
                self.active_index = (self.active_index - 1) % len(self.view.fields)
            return None
        if key.name == "esc":
            return {"cancel": True}
        if key.name in ("ctrl+s", "ctrl+enter"):
            return self._submit()

        if not self.view.fields:
            return None

        current_field = self.view.fields[self.active_index]
        name = current_field.name
        ftype = current_field.field_type
        val = self.values.get(name, "")

        if current_field.options is not None:
            options = [option.id for option in current_field.options]
            if options and key.name in ("left", "right", "space"):
                index = options.index(val) if val in options else (-1 if key.name != "left" else 0)
                self.values[name] = options[(index + (-1 if key.name == "left" else 1)) % len(options)]
                self.dirty_fields.add(name)
                return None
            if key.name != "enter":
                if key.text:
                    match = next((option.id for option in current_field.options if option.label.casefold().startswith(key.text.casefold())), None)
                    if match is None:
                        match = next((option for option in options if option.casefold().startswith(key.text.casefold())), None)
                    if match is not None:
                        self.values[name] = match
                        self.dirty_fields.add(name)
                return None

        if ftype == "boolean":
            if key.name in ("space", "enter"):
                # Tri-state cycle: unset -> true -> false -> unset.
                self.values[name] = {None: True, True: False, False: None}.get(self.values.get(name) if name in self.values else None)
                self.dirty_fields.add(name)
            return None

        if key.name == "enter":
            if ftype == "multiline":
                self.values[name] = str(val) + "\n"
                self.dirty_fields.add(name)
                return None
            if self.active_index == len(self.view.fields) - 1:
                return self._submit()
            self.active_index += 1
            return None

        if key.name == "backspace":
            sval = "" if val is None else str(val)
            if sval:
                self.values[name] = sval[:-1]
                self.dirty_fields.add(name)
            return None

        if key.text and not key.name.startswith("ctrl+") and not key.name.startswith("alt+"):
            # Numeric fields stay raw text: nothing is parsed or coerced while
            # typing, so "1e3" is never silently truncated to 1 and cannot be
            # auto-submitted as a different number than the user typed.
            # A present None is an empty editor: typing must not append to "None".
            self.values[name] = ("" if val is None else str(val)) + key.text
            self.dirty_fields.add(name)
            return None

        return None

    def _submit(self) -> dict[str, Any] | None:
        try:
            arguments = validate_form_submission(self.view, self.values, action_schema=self._action_schema)
        except ValueError as exc:
            self.validation_error = str(exc)
            return None
        return {"action": self.view.action, "arguments": arguments}

    def render_rows(self, width: int, styles: Styles | None = None) -> list[str]:
        resolved = _styles_arg(styles)
        fg, bg, tone_slot = resolve_component(self.view.style)
        text = Text()
        if self.load_error:
            text.append(f"[warn] Load failure: {escape_terminal_controls(self.load_error)}\n", style=resolved.style("error", bold=True))
        if self.validation_error:
            text.append(f"[error] Validation: {escape_terminal_controls(self.validation_error)}\n", style=resolved.style("error", bold=True))

        text.append(
            "Enter/Tab next · Ctrl+S submit · Esc cancel\n\n",
            style=resolved.style(tone_slot or "dim", color=fg, background=bg),
        )
        for i, field in enumerate(self.view.fields):
            name = field.name
            label = escape_terminal_controls(field.label)
            ftype = field.field_type
            raw = self.values.get(name, "")
            val = escape_terminal_controls("" if raw is None else str(raw), preserve_newlines=True)
            is_active = i == self.active_index
            req_mark = "*" if field.required else ""

            prefix = "> " if is_active else "  "
            if is_active:
                label_style = resolved.style(tone_slot or "accent", color=fg, background=bg, bold=True)
            else:
                label_style = resolved.style(tone_slot or "text", color=fg, background=bg)
            text.append(f"{prefix}{label}{req_mark}: ", style=label_style)

            if field.options is not None:
                selected = next((option.label for option in field.options if option.id == raw), "(unset)")
                text.append(escape_terminal_controls(selected) + " (←/→ select)\n", style=resolved.style("text"))
            elif ftype == "boolean":
                if raw is True:
                    bool_str = "[X] True"
                elif raw is False:
                    bool_str = "[ ] False"
                else:
                    bool_str = "[ ] (unset)"
                present = raw is not None
                if raw is True:
                    value_style = resolved.style("success", bold=True)
                elif present:
                    value_style = resolved.style("dim")
                else:
                    value_style = resolved.style("text", italic=True)
                text.append(f"{bool_str}\n", style=value_style)
            elif ftype == "multiline":
                lines = val.split("\n")
                value_style = resolved.style("warning") if is_active else resolved.style("dim")
                if len(lines) <= 1:
                    text.append(f"'{val}'\n", style=value_style)
                else:
                    text.append("\n", style=resolved.style("text"))
                    for line in lines:
                        text.append(f"    | {line}\n", style=value_style)
            else:
                text.append(f"'{val}'\n", style=resolved.style("warning") if is_active else resolved.style("dim"))

        panel = Panel(
            text,
            title=escape_terminal_controls(self.title),
            border_style=resolved.style(tone_slot or "accent", color=fg, background=bg),
            expand=True,
        )
        return render_lines(panel, width, theme=markdown_theme(), color_system=resolved.color_system)


def render_plugin_view(descriptor: PluginViewDescriptor, width: int, styles: Styles | None = None) -> list[str]:
    """Render any of the 11 finite view kinds into ANSI rows.

    Raises UnsupportedViewError if the view_type is outside the 11 finite kinds.
    """
    data = parse_view_data(descriptor.view_type, descriptor.data)

    if isinstance(data, NoticeViewData):
        return render_notice(data, width, styles)
    if isinstance(data, MarkdownViewData):
        return render_markdown(data, width, styles)
    if isinstance(data, FieldsViewData):
        return render_fields(data, width, styles)
    if isinstance(data, DiffViewData):
        return render_diff(data, width, styles)
    if isinstance(data, StatusViewData):
        return render_status(data, width, styles)
    if isinstance(data, TableViewData):
        return render_table(data, width, styles)
    if isinstance(data, JsonViewData):
        return render_json(data, width, styles)
    if isinstance(data, MediaViewData):
        return render_media(data, width, styles)
    if isinstance(data, ChoicesViewData):
        return InteractiveChoices(data).render_rows(width, styles)
    if isinstance(data, ConfirmationViewData):
        return InteractiveConfirmation(data).render_rows(width, styles)
    if isinstance(data, FormViewData):
        return InteractiveForm(data).render_rows(width, styles)

    raise UnsupportedViewError(f"unsupported view type: {descriptor.view_type}")
