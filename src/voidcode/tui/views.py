"""Pure renderers and interactive models for the 11 finite core plugin UI kinds."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from voidcode.sdk.ask import AskMultiSelect, AskRequest
from voidcode.sdk.icons import Icon
from voidcode.sdk.json_values import json_wire_value
from voidcode.sdk.tools import (
    AttachmentOutput,
    CompositeOutput,
    ContentPart,
    EmptyOutput,
    JsonPart,
    MediaPart,
    TablePart,
    TextOutput,
    TextPart,
    ToolBody,
    ToolFailure,
    ToolResult,
)
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
from voidcode.tui.gates import ApprovalGate

from .formatting import tone_prefix, tone_slot
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
    fg, bg, component_slot = resolve_component(data.style, level=data.level)
    prefix = tone_prefix(data.level)
    slot = component_slot or tone_slot(data.level)
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


def render_thinking(text: str, width: int, styles: Styles | None = None, *, collapsed: bool = True) -> list[str]:
    """Reasoning trace as compact summary by default, or full muted italic text."""
    resolved = _styles_arg(styles)
    clean = escape_terminal_controls(text, preserve_newlines=True, preserve_tabs=True)
    if collapsed:
        first_line = clean.strip().splitlines()[0] if clean.strip() else ""
        if len(first_line) > 60:
            first_line = first_line[:57] + "…"
        summary = f"💭 Thought ({len(clean)} chars) · {first_line}" if first_line else f"💭 Thought ({len(clean)} chars)"
        return [resolved.fg("dim", summary)]
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


def _content_part_text(part: ContentPart) -> str:
    """One typed composite part's ordered readable text with reference URIs."""
    if isinstance(part, TextPart):
        if part.reference is not None:
            suffix = f"[full text: {part.reference.uri}]"
            return f"{part.text}\n{suffix}" if part.text else suffix
        return part.text
    if isinstance(part, MediaPart):
        mime_text = part.mime
        label = "Image" if mime_text.startswith("image/") else "Audio" if mime_text.startswith("audio/") else "Media"
        return f"[{label}: {mime_text}, {part.reference.bytes} bytes, {part.reference.uri}] {part.presentation}"
    if isinstance(part, TablePart):
        lines = ["\t".join(part.columns)]
        lines.extend("\t".join(row) for row in part.rows)
        if part.presentation:
            lines.insert(0, part.presentation)
        if part.total_rows is not None and part.total_rows > len(part.rows):
            if part.reference is None:  # rejected by TablePart.__post_init__
                raise ValueError("truncated table requires a reference")
            lines.append(f"[showing {len(part.rows)}/{part.total_rows} rows; full table: {part.reference.uri}]")
        return "\n".join(lines)
    if isinstance(part, JsonPart):
        return f"{part.preview}\n[full JSON: {part.reference.uri}]"
    raise ValueError(f"unsupported composite part: {type(part).__name__}")


def _content_part_has_reference(part: ContentPart) -> bool:
    if isinstance(part, (MediaPart, JsonPart)):
        return True
    return isinstance(part, (TextPart, TablePart)) and part.reference is not None


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
#: Bound the gutter for hostile hunk headers: valid diffs never need more.
_MAX_DIFF_LINE_DIGITS = 6


def _diff_line_layout(diff_lines: Sequence[str]) -> list[tuple[str, str]]:
    """Classify each unified diff line and build its bounded ``old new`` gutter.

    Returns ``(gutter, kind)`` aligned with ``diff_lines``; kinds are ``added``,
    ``removed``, ``hunk``, and ``context``. Hunk body lines are numbered from
    the header starts, while headers and out-of-hunk text stay unnumbered. The
    gutter follows the widest line number actually shown, capped at six digits,
    so untrusted diff text cannot widen a card.
    """
    numbers: list[tuple[int | None, int | None]] = []
    kinds: list[str] = []
    old = new = 0
    in_hunk = False
    for line in diff_lines:
        header = _HUNK_HEADER.match(line)
        if header is not None:
            old, new = int(header.group(1)), int(header.group(2))
            in_hunk = True
            numbers.append((None, None))
            kinds.append("hunk")
            continue
        if line.startswith("\\"):
            numbers.append((None, None))
            kinds.append("context")
            continue
        if not in_hunk or line.startswith(("---", "+++")):
            in_hunk = False
            numbers.append((None, None))
            kinds.append("context")
            continue
        if line.startswith("+") and not line.startswith("+++"):
            numbers.append((None, new))
            kinds.append("added")
            new += 1
        elif line.startswith("-") and not line.startswith("---"):
            numbers.append((old, None))
            kinds.append("removed")
            old += 1
        elif line.startswith(" "):
            numbers.append((old, new))
            kinds.append("context")
            old += 1
            new += 1
        else:
            in_hunk = False
            numbers.append((None, None))
            kinds.append("context")
    highest = max((number for pair in numbers for number in pair if number is not None), default=0)
    width = min(_MAX_DIFF_LINE_DIGITS, max(1, len(str(highest))))
    gutters = [f"{_bounded_line_number(old_number, width)} {_bounded_line_number(new_number, width)} " for old_number, new_number in numbers]
    return [
        ("" if old_number is None and new_number is None else gutter, kind)
        for (old_number, new_number), gutter, kind in zip(numbers, gutters, kinds, strict=True)
    ]


def _bounded_line_number(number: int | None, width: int) -> str:
    if number is None:
        return " " * width
    text = str(number)
    if len(text) > width:
        text = "…" + text[-(width - 1) :] if width > 1 else "…"
    return text.rjust(width)


def _render_diff_body(body: ToolBody | None, width: int, styles: Styles) -> list[str] | None:
    """Single-file edit semantics as a diff card, or None without complete shape.

    Shape dispatch only (path/diff/additions/deletions with strict types): no
    tool-name guessing, no plugin imports. Hunk styling reuses the DiffViewData
    slots; complete structured data stays on the existing result_reference and
    artifact entries, never a second full-text copy. Incomplete shapes return
    None so output-only rendering is preserved; diff text is escaped so
    scrollback stays readable.
    """
    if body is None:
        return None
    payload = body.as_payload()
    path = payload.get("path")
    diff_text = payload.get("diff")
    additions = payload.get("additions")
    deletions = payload.get("deletions")
    if not isinstance(path, str) or not path:
        return None
    if not isinstance(diff_text, str) or not diff_text:
        return None
    if type(additions) is not int or additions < 0:
        return None
    if type(deletions) is not int or deletions < 0:
        return None
    card = Text()
    card.append(f"Diff: {escape_terminal_controls(path)} (+{additions} -{deletions})", style=styles.style("text", bold=True))
    diff_lines = diff_text.splitlines()
    for (gutter, kind), line in zip(_diff_line_layout(diff_lines), diff_lines, strict=True):
        card.append("\n")
        if gutter:
            card.append(gutter, style=styles.style("dim"))
        clean = escape_terminal_controls(line)
        if kind == "added":
            card.append(clean, style=styles.style("diff_added"))
        elif kind == "removed":
            card.append(clean, style=styles.style("diff_removed"))
        elif kind == "hunk":
            card.append(clean, style=styles.style("accent"))
        else:
            card.append(clean, style=styles.style("diff_context"))
    return render_lines(card, width, theme=markdown_theme(), color_system=styles.color_system)


def render_tool_result(
    result: ToolResult,
    width: int,
    styles: Styles | None = None,
    *,
    interrupted: bool = False,
) -> list[str]:
    """Preserve selected inline output; immutable source paging is a separate action."""
    resolved = _styles_arg(styles)
    failed = interrupted or isinstance(result, ToolFailure)
    rows: list[str] = []
    output = result.output
    bodies: list[str] = []
    suffix = ""
    artifact_available = result.result_reference is not None
    if isinstance(output, TextOutput):
        bodies.append(output.presentation if output.presentation else output.text)
    elif isinstance(output, AttachmentOutput):
        bodies.append(f"[{output.mime}]")
    elif isinstance(output, EmptyOutput):
        bodies.append(output.presentation or "")
    elif isinstance(output, CompositeOutput):
        for part in output.parts:
            bodies.append(_content_part_text(part))
            artifact_available = artifact_available or _content_part_has_reference(part)
    else:
        raise ValueError(f"unsupported tool output: {type(output).__name__}")
    bounds = output.bounds
    flags = [name for name, value in (("truncated", bounds.truncated), ("partial", bounds.partial)) if value]
    artifact_available = artifact_available or bounds.reference is not None
    note = ", ".join([*flags, bounds.source] if bounds.source else flags)
    if note:
        suffix = f"[{note}]"
    body = "\n\n".join(block for block in bodies if block)
    if failed:
        if body and isinstance(output, CompositeOutput):
            rows.extend(
                render_lines(
                    Text(escape_terminal_controls(body, preserve_newlines=True, preserve_tabs=True), style=resolved.style("dim")),
                    width,
                    theme=markdown_theme(),
                    color_system=resolved.color_system,
                )
            )
        if isinstance(result, ToolFailure):
            error = result.error
        else:
            error = "interrupted"
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
    if not failed:
        diff_rows = _render_diff_body(result.body, width, resolved)
        if diff_rows:
            rows.extend(diff_rows)
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
    for (gutter, kind), line in zip(_diff_line_layout(diff_lines), diff_lines, strict=True):
        diff_text.append("\n")
        if gutter:
            diff_text.append(gutter, style=resolved.style("dim"))
        clean = escape_terminal_controls(line)
        if kind == "added":
            diff_text.append(clean, style=resolved.style("diff_added"))
        elif kind == "removed":
            diff_text.append(clean, style=resolved.style("diff_removed"))
        elif kind == "hunk":
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

    The decision outcome: ``{"request_id", "decision", "grant_session_policy", "reason"}``.
    """

    def __init__(self, gate: ApprovalGate, *, icon: Icon | None = None) -> None:
        self.gate = gate
        self.icon = icon
        self.request_id = gate.request_id
        self.tool_name = gate.tool_name
        self.arguments: Mapping[str, Any] = gate.call.arguments
        self.scroll = 0
        self.reason_mode: bool = False
        self.reason_input: str = ""

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        if self.reason_mode:
            if key.name == "escape":
                self.reason_mode = False
                self.reason_input = ""
                return None
            if key.name in ("enter", "return"):
                reason = self.reason_input.strip() or "denied by user"
                return {"request_id": self.request_id, "decision": "deny", "reason": reason}
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
            return {"request_id": self.request_id, "decision": "allow", "grant_session_policy": True}
        if key.name == "r":
            self.reason_mode = True
            self.reason_input = ""
            return None
        if key.name in ("a", "enter"):
            return {"request_id": self.request_id, "decision": "allow", "grant_session_policy": False}
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
        if self.reason_mode:
            visible.append(resolved.fg("warning", f"  Denial reason: {escape_terminal_controls(self.reason_input)}█"))
            visible.append(resolved.fg("dim", "  [Enter] submit denial with reason    [Esc] cancel"))
        else:
            visible.append(resolved.fg("text", "  [a] allow once    [s] allow for session    [d] deny    [r] deny with reason    [up/down] scroll"))
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


class InteractiveAsk:
    """Interactive Ask dialog collecting answers for one model-initiated AskRequest."""

    def __init__(self, request: AskRequest, *, revision_id: str = "") -> None:
        self.request = request
        self.revision_id = revision_id
        self.request_id = request.request_id
        self.title = request.title
        self.questions = request.questions
        self.active_index = 0
        self.values: dict[str, Any] = {}
        self.multiselect_focused_opt: dict[str, int] = {}
        self.validation_error: str | None = None

        for q in self.questions:
            if isinstance(q, AskMultiSelect):
                self.values[q.name] = []
                self.multiselect_focused_opt[q.name] = 0

    def handle_key(self, key: Key) -> dict[str, Any] | None:
        self.validation_error = None
        if key.name in ("tab", "down"):
            if self.questions:
                self.active_index = (self.active_index + 1) % len(self.questions)
            return None
        if key.name in ("shift+tab", "up"):
            if self.questions:
                self.active_index = (self.active_index - 1) % len(self.questions)
            return None
        if key.name == "esc":
            return {"request_id": self.request_id, "action": "cancel"}
        if key.name in ("ctrl+s", "ctrl+enter"):
            return self._submit()

        if not self.questions:
            return None

        current = self.questions[self.active_index]
        name = current.name

        if isinstance(current, AskMultiSelect):
            opt_idx = self.multiselect_focused_opt.get(name, 0)
            if key.name in ("left", "h"):
                self.multiselect_focused_opt[name] = (opt_idx - 1) % len(current.options)
                return None
            if key.name in ("right", "l"):
                self.multiselect_focused_opt[name] = (opt_idx + 1) % len(current.options)
                return None
            if key.name == "space":
                chosen = list(self.values.get(name, []))
                opt_id = current.options[opt_idx].id
                if opt_id in chosen:
                    chosen.remove(opt_id)
                else:
                    chosen.append(opt_id)
                self.values[name] = chosen
                return None
            if key.name == "enter":
                if self.active_index == len(self.questions) - 1:
                    return self._submit()
                self.active_index += 1
                return None
            return None

        ftype = current.field_type
        val = self.values.get(name, "")
        if current.options is not None:
            options = [option.id for option in current.options]
            if options and key.name in ("left", "right", "space"):
                index = options.index(val) if val in options else (-1 if key.name != "left" else 0)
                self.values[name] = options[(index + (-1 if key.name == "left" else 1)) % len(options)]
                return None
            if key.name != "enter":
                return None

        if ftype == "boolean":
            if key.name in ("space", "enter"):
                self.values[name] = not bool(self.values.get(name, False))
            return None

        if key.name == "enter":
            if ftype == "multiline":
                self.values[name] = str(val) + "\n"
                return None
            if self.active_index == len(self.questions) - 1:
                return self._submit()
            self.active_index += 1
            return None

        if key.name == "backspace":
            sval = "" if val is None else str(val)
            if sval:
                self.values[name] = sval[:-1]
            return None

        if key.text and not key.name.startswith("ctrl+") and not key.name.startswith("alt+"):
            self.values[name] = ("" if val is None else str(val)) + key.text
            return None

        return None

    def _submit(self) -> dict[str, Any] | None:
        for q in self.questions:
            if isinstance(q, AskMultiSelect):
                chosen = self.values.get(q.name, [])
                if q.required and not chosen:
                    self.validation_error = f"'{q.label}' requires at least 1 selection"
                    return None
                if len(chosen) < q.min_selections:
                    self.validation_error = f"'{q.label}' requires at least {q.min_selections} selections"
                    return None
                if q.max_selections is not None and len(chosen) > q.max_selections:
                    self.validation_error = f"'{q.label}' allows at most {q.max_selections} selections"
                    return None
            else:
                val = self.values.get(q.name)
                if q.required and (val is None or val == ""):
                    self.validation_error = f"'{q.label}' is required"
                    return None
        return {"request_id": self.request_id, "action": "answer", "values": dict(self.values)}

    def render_rows(self, width: int, styles: Styles | None = None) -> list[str]:
        resolved = _styles_arg(styles)
        text = Text()
        text.append(f"{escape_terminal_controls(self.title)}\n", style=resolved.style("accent", bold=True))
        text.append(f"Request: {escape_terminal_controls(self.request_id[:12])}\n\n", style=resolved.style("dim"))

        for i, q in enumerate(self.questions):
            is_active = i == self.active_index
            prefix = "❯ " if is_active else "  "
            cursor_style = resolved.style("accent", bold=True) if is_active else resolved.style("dim")
            label_style = resolved.style("text", bold=is_active)
            text.append(prefix, style=cursor_style)
            text.append(f"{escape_terminal_controls(q.label)}", style=label_style)
            if getattr(q, "required", False):
                text.append(" *", style=resolved.style("error"))
            text.append(": ")

            if isinstance(q, AskMultiSelect):
                chosen = set(self.values.get(q.name, []))
                f_idx = self.multiselect_focused_opt.get(q.name, 0)
                text.append("\n")
                for o_idx, opt in enumerate(q.options):
                    is_opt_focused = is_active and (o_idx == f_idx)
                    box = "[x]" if opt.id in chosen else "[ ]"
                    opt_style = resolved.style("warning" if is_opt_focused else ("accent" if opt.id in chosen else "dim"))
                    text.append(f"    {box} {escape_terminal_controls(opt.label)}", style=opt_style)
                    if o_idx < len(q.options) - 1:
                        text.append("  ")
                text.append("\n")
            elif getattr(q, "options", None) is not None:
                val = self.values.get(q.name, "")
                text.append(f"<{escape_terminal_controls(str(val))}>\n", style=resolved.style("warning" if is_active else "dim"))
            elif getattr(q, "field_type", None) == "boolean":
                val = self.values.get(q.name)
                b_str = "true" if val is True else "false" if val is False else "unset"
                text.append(f"[{b_str}]\n", style=resolved.style("warning" if is_active else "dim"))
            else:
                val = self.values.get(q.name, "")
                text.append(f"'{escape_terminal_controls(str(val))}'\n", style=resolved.style("warning" if is_active else "dim"))

        if self.validation_error:
            text.append(f"\n  Error: {escape_terminal_controls(self.validation_error)}\n", style=resolved.style("error", bold=True))

        text.append("\n[Enter] next/submit    [Space] toggle/select    [Esc] cancel\n", style=resolved.style("dim"))
        panel = Panel(
            text,
            title="Interactive Ask",
            border_style=resolved.style("accent"),
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
