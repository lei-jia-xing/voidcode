"""Pure ANSI row renderers for plugin content, tools, and transcript entries."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Sequence

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

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
    DiffViewData,
    FieldsViewData,
    JsonViewData,
    MarkdownViewData,
    MediaViewData,
    NoticeViewData,
    StatusViewData,
    TableViewData,
)

from .formatting import tone_prefix, tone_slot
from .styles import Styles, markdown_theme, render_icon, resolve_component, syntax_theme
from .term import escape_terminal_controls, render_lines


def _styles_arg(styles: Styles | None) -> Styles:
    """The caller's styles, or a default-depth handle when none travels with the call."""
    return styles if styles is not None else Styles()


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
