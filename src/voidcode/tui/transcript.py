"""Transcript tape: typed blocks rendered to fixed-width ANSI rows.

Visual grammar ported from oh-my-pi (MIT; ``/tmp/omp-src``), ``path:line``
relative to that checkout:

* Exactly one blank row between non-empty blocks, block blank edges trimmed:
  ``packages/tui/src/chrome/transcript-container.ts:117-138, 618-624``.
* User turn = full-width background band, horizontal padding 1:
  ``packages/tui/src/chat/user-message.ts:135-141`` (bg token
  ``userMessageBg``), ``components/markdown.ts:2164-2165``.
* Assistant prose = plain markdown, padding x=1 / y=0, no header, no background:
  ``chat/assistant-message.ts:1111-1116``; the only live row is the
  hidden-thinking pulse ``chat/assistant-message.ts:1157-1161, 498-521``.
* Thinking = italic prose in ``thinkingText``:
  ``chat/assistant-message.ts:1137-1141``.
* Tool header = ``icon + ' ' + title``, optional ``': ' + description``, then
  meta joined by ``theme.sep.dot``: ``render/status-line.ts:33-52``.
* Framed tool card (``╭── header ──╮`` / ``│ body │`` / ``╰────╯``, border and
  background by state): ``render/output-block.ts:79-220``.
* Streaming tail row ``… (streaming)`` carries the animated glyph; the header
  drops its icon while partial: ``tools/write.ts:282-285``,
  ``tools/edit.ts:1136``.
* Preview budgets: ``render/render-utils.ts:98-115``; truncation lengths
  ``:184-196``.
* Diff gutter (fixed 3-digit line-number field, ``+NNN│content``):
  ``chrome/diff.ts:118-147``, ``render/render-utils.ts:429-438``; leading
  whitespace visualiser ``chrome/diff.ts:14-30``.
* Expand hints from the keybinding table: ``render/render-utils.ts:199-208,
  301-309, 982-985``; counted form ``chat/execution-shared.ts:86-88``; key
  labels ``app-keybindings.ts:695-757``.
* History-collapse divider (session markers):
  ``chat/compaction-summary-message.ts:39-72``.

This module is pure: it renders rows and writes nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, cast

from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from rich.markdown import Markdown
from rich.syntax import Syntax

from .term import clamp_row, format_number, render_lines, visible_width, wrap_row
from .theme import Theme

__all__ = [
    "PREVIEW_LIMITS",
    "TRUNCATE_LENGTHS",
    "AssistantBlock",
    "BackgroundTaskBlock",
    "Block",
    "DiffBlock",
    "ErrorBlock",
    "KeyHints",
    "NoticeBlock",
    "SessionMarkerBlock",
    "ThinkingBlock",
    "ToolBlock",
    "Transcript",
    "UserBlock",
    "format_code_frame_line",
    "render_diff",
    "format_key_hint",
    "format_number",
    "short_session_id",
]

#: ``PREVIEW_LIMITS`` (``render/render-utils.ts:98-115``), ported verbatim.
PREVIEW_LIMITS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "OUTPUT_COLLAPSED": 3,
        "OUTPUT_EXPANDED": 10,
        "DIFF_COLLAPSED_LINES": 40,
    }
)

#: ``TRUNCATE_LENGTHS`` (``render/render-utils.ts:184-196``).
TRUNCATE_LENGTHS: Final[Mapping[str, int]] = MappingProxyType({"TITLE": 60, "CONTENT": 80, "LINE": 110})

#: Tab expansion width (``DEFAULT_TAB_WIDTH``).
TAB_WIDTH: Final = 4

#: Content padding inside a framed card (``render/output-block.ts:50-53``).
CONTENT_PADDING: Final = 1

_INDENT = "\x1b[2m"
_INDENT_OFF = "\x1b[22m"

#: ``KEY_LABELS`` (``app-keybindings.ts:716-733``).
KEY_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "esc": "Esc",
        "escape": "Esc",
        "enter": "Enter",
        "return": "Enter",
        "space": "Space",
        "tab": "Tab",
        "backspace": "Backspace",
        "delete": "Delete",
        "home": "Home",
        "end": "End",
        "pageup": "PgUp",
        "pagedown": "PgDn",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
    }
)

_MODIFIERS: Final[frozenset[str]] = frozenset({"ctrl", "shift", "alt", "super"})


def format_key_hint(key: str, platform: str | None = None) -> str:
    """Human-readable key chord, e.g. ``ctrl+o`` -> ``Ctrl+O``.

    Ports ``formatKeyHint``/``formatKeyPart`` (``app-keybindings.ts:735-751``),
    including the macOS ``Option``/``Cmd`` labels.
    """
    system = platform if platform is not None else sys.platform
    return "+".join(_format_key_part(part, system) for part in key.split("+"))


def short_session_id(session_id: str | None) -> str:
    """Display form of a session id: strip the ``session-`` prefix, keep 8 chars."""
    return session_id.removeprefix("session-")[:8] if session_id else ""


def _format_key_part(part: str, platform: str) -> str:
    lower = part.lower()
    if lower in _MODIFIERS:
        if lower == "ctrl":
            return "Ctrl"
        if lower == "shift":
            return "Shift"
        if lower == "alt":
            return "Option" if platform == "darwin" else "Alt"
        return "Cmd" if platform == "darwin" else "Super"
    label = KEY_LABELS.get(lower)
    if label:
        return label
    if len(part) == 1:
        return part.upper()
    return part[:1].upper() + part[1:]


@dataclass(frozen=True, slots=True)
class KeyHints:
    """The keybinding table the transcript generates its hints from.

    omp resolves the key through the keybinding manager
    (``render/render-utils.ts:199-208``); the transcript never hard-codes one.
    """

    expand: str = "ctrl+o"

    def key_label(self) -> str:
        """Formatted expand key, e.g. ``Ctrl+O``."""
        return format_key_hint(self.expand)

    def expand_hint(self, theme: Theme, expanded: bool, has_more: bool = True) -> str:
        """``⟦Ctrl+O: Expand⟧`` -- empty when expanded or nothing is hidden.

        ``formatExpandHint`` (``render/render-utils.ts:301-309``) with the
        bracket glyphs taken from the theme's ``format.bracket*`` symbols.
        """
        if expanded or not has_more:
            return ""
        left = theme.symbol("format.bracketLeft")
        right = theme.symbol("format.bracketRight")
        return theme.fg("dim", f"{left}{self.key_label()}: Expand{right}")

    def more_lines_hint(self, theme: Theme, count: int) -> str:
        """``… N more lines (ctrl+o to expand)`` (``execution-shared.ts:86-88``)."""
        if count <= 0:
            return ""
        return theme.fg("dim", f"… {count} more lines ({self.expand} to expand)")


def replace_tabs(text: str) -> str:
    """Expand tabs to ``TAB_WIDTH`` spaces (``replaceTabs``, ``utils.ts:199-201``)."""
    return text.replace("\t", " " * TAB_WIDTH)


def format_code_frame_line(marker: str, line_number: str | int, content: str, line_number_width: int) -> str:
    """One diff row: ``+NNN│content`` (``render-utils.ts:429-438``).

    The gutter field is ``line_number_width + 1`` cells wide, so added rows read
    ``+123│``, removed ``-123│`` and context rows `` 123│``. A constant width
    (minimum 3) keeps streamed rows byte-identical to the settled render
    (``chrome/diff.ts:118-127``): a width derived from the current maximum line
    number would widen at the 100-line crossing and re-pad every rendered row.
    """
    marker_text = marker.strip()
    number_text = str(line_number).strip()
    if marker_text and number_text:
        gutter = f"{marker_text}{number_text}"
    else:
        gutter = number_text or marker_text
    return f"{gutter.rjust(line_number_width + 1)}│{content}"


def _visualize_indent(text: str) -> str:
    """Show leading whitespace with dim glyphs (``chrome/diff.ts:14-30``)."""
    stripped = text.lstrip(" \t")
    indent = text[: len(text) - len(stripped)]
    if not indent:
        return replace_tabs(text)
    left_padding = TAB_WIDTH // 2
    right_padding = max(0, TAB_WIDTH - left_padding - 1)
    tab_marker = f"{_INDENT}{' ' * left_padding}→{' ' * right_padding}{_INDENT_OFF}"
    visible = "".join(tab_marker if char == "\t" else f"{_INDENT}·{_INDENT_OFF}" for char in indent)
    return f"{visible}{replace_tabs(stripped)}"


def _pad_to(text: str, width: int) -> str:
    """Pad (or clamp) ``text`` to exactly ``width`` cells (``render/utils.ts:94-102``)."""
    current = visible_width(text)
    if current == width:
        return text
    if current < width:
        return text + " " * (width - current)
    return clamp_row(text, width)


class _Renderer:
    """Per-render context shared by every block."""

    __slots__ = ("hints", "theme", "width")

    def __init__(self, theme: Theme, width: int, hints: KeyHints) -> None:
        self.theme = theme
        self.width = max(1, width)
        self.hints = hints

    def markdown(self, text: str, width: int) -> list[str]:
        """Render markdown to rows at ``width`` using the theme's style table."""
        # rich annotates ``Markdown.code_theme`` as ``str`` but forwards it to
        # ``Syntax(theme=...)``, which takes a ``SyntaxTheme``; the token-based
        # theme keeps fenced code on the same palette as tool bodies.
        document = Markdown(text, code_theme=cast("str", self.theme.syntax_theme()))
        rows = render_lines(
            document,
            width,
            theme=self.theme.rich_theme(),
            color_system=self.theme.color_system,
        )
        # rich pads rendered rows out to the console width; the transcript rows
        # are fitted by the frame, so the padding is dropped here.
        return [row.rstrip() for row in rows]

    def code(self, text: str, width: int, language: str | None) -> list[str]:
        """Syntax-highlight ``text`` at ``width`` with the themed syntax table."""
        lexer = "text"
        if language:
            try:
                get_lexer_by_name(language)
            except ClassNotFound:
                lexer = "text"
            else:
                lexer = language
        rows = render_lines(
            Syntax(text, lexer, theme=self.theme.syntax_theme()),
            width,
            theme=self.theme.rich_theme(),
            color_system=self.theme.color_system,
        )
        return [row.rstrip() for row in rows]

    def body_line(self, line: str, width: int) -> list[str]:
        """Wrap one body/preview line to ``width`` cells."""
        return wrap_row(line, width, theme=self.theme.rich_theme(), color_system=self.theme.color_system)

    def dim(self, text: str) -> str:
        """Dim-coloured text (``theme.fg("dim", ...)``)."""
        return self.theme.fg("dim", text)


@dataclass
class Block:
    """Base transcript block.

    ``settled`` marks the block final: the tape hands settled rows to the commit
    path and keeps only the non-settled tail live.
    """

    expanded: bool = False
    settled: bool = True

    def render(self, renderer: _Renderer) -> list[str]:  # pragma: no cover - overridden
        """Rows for this block at the renderer's width."""
        raise NotImplementedError


@dataclass
class UserBlock(Block):
    """User turn: full-width background band, one column of horizontal padding."""

    text: str = ""

    def render(self, renderer: _Renderer) -> list[str]:
        theme = renderer.theme
        inner_width = max(1, renderer.width - 2)
        rows: list[str] = []
        for row in renderer.markdown(self.text, inner_width):
            band = _pad_to(f" {row} ", renderer.width)
            rows.append(theme.bg_fill("userMessageBg", theme.fg_resolved("userMessageText", band)))
        return rows


@dataclass
class AssistantBlock(Block):
    """Assistant prose: borderless markdown plus an optional thinking pulse row."""

    text: str = ""
    streaming: bool = False
    #: Spinner frame for the hidden-thinking pulse; ``None`` hides the row.
    pulse_frame: int | None = None
    pulse_tokens: int | None = None
    pulse_rate: float | None = None

    def render(self, renderer: _Renderer) -> list[str]:
        inner_width = max(1, renderer.width - 2)
        rows = [f" {row}" for row in renderer.markdown(self.text, inner_width)]
        if self.streaming and self.pulse_frame is not None:
            rows.append(f" {self.pulse_row(renderer)}")
        return rows

    def pulse_row(self, renderer: _Renderer) -> str:
        """The hidden-thinking pulse row (``assistant-message.ts:498-521``).

        ``<glyph> Thinking[ · <tokens>][ <rate>toks/s]`` -- the only animated row
        an assistant block carries, and it is always the last one, so a native
        scrollback commit boundary is never pinned to an animating head row.
        """
        theme = renderer.theme
        glyph = theme.glyphs.spinner("status", self.pulse_frame or 0)
        row = f"{theme.fg('thinkingText', glyph)}{theme.fg('muted', ' Thinking')}"
        if self.pulse_tokens:
            row += theme.fg("dim", f" · {format_number(self.pulse_tokens)}")
        if self.pulse_rate is not None and self.pulse_rate >= 0.05:
            row += theme.fg("dim", f" {self.pulse_rate:.1f} tok/s")
        return row


@dataclass
class ThinkingBlock(Block):
    """Reasoning text: italic ``thinkingText`` prose, collapsible to one row.

    omp hides reasoning entirely once the answer starts
    (``assistant-message.ts`` ``#hideThinkingBlock``); voidcode keeps it
    reachable behind ``ctrl+o``, so the collapsed form is a single dim summary
    row carrying the line count and the table-driven expand hint.
    """

    text: str = ""
    level: str = "medium"

    def render(self, renderer: _Renderer) -> list[str]:
        theme = renderer.theme
        if not self.expanded:
            lines = len(self.text.splitlines()) or 1
            glyph = theme.symbol(f"thinking.{self.level}") or theme.symbol("thinking.medium")
            summary = theme.fg("dim", f"{glyph} Thinking · {lines} {'line' if lines == 1 else 'lines'}")
            hint = renderer.hints.expand_hint(theme, expanded=False)
            trailer = f" {hint}" if hint else ""
            return [f" {summary}{trailer}"]
        rows = renderer.markdown(self.text, max(1, renderer.width - 2))
        return [f" \x1b[3m{theme.fg('thinkingText', row)}\x1b[0m" for row in rows]


@dataclass
class ToolBlock(Block):
    """Tool call: one-line header, collapsible body, framed card when expanded."""

    tool: str = "default"
    title: str = ""
    summary: str = ""
    body: Sequence[str] = ()
    language: str | None = None
    state: str = "pending"
    streaming: bool = False
    spinner_frame: int = 0
    #: ``False`` renders an unframed header + preview (omp's plain tool card).
    frame: bool = True

    def render(self, renderer: _Renderer) -> list[str]:
        theme = renderer.theme
        header = self._header(renderer)
        body = list(self.body)
        limit = PREVIEW_LIMITS["OUTPUT_EXPANDED"] if self.expanded else PREVIEW_LIMITS["OUTPUT_COLLAPSED"]
        visible = body[:limit]
        hidden = max(0, len(body) - len(visible))
        if self.frame and self.expanded:
            rows = self._framed(renderer, header, visible)
            if hidden:
                rows.append(f" {renderer.hints.more_lines_hint(theme, hidden)}")
        else:
            rows = [header]
            rows.extend(self._body_rows(renderer, visible))
            if hidden:
                rows.append(f" {renderer.hints.more_lines_hint(theme, hidden)}")
        if self.streaming:
            rows.append(self._streaming_row(renderer))
        return rows

    def _streaming_row(self, renderer: _Renderer) -> str:
        """Trailing liveness row (``tools/write.ts:282-285``).

        The animated glyph rides here -- never on a framed card's header row,
        which would pin the native-scrollback commit boundary to the top of the
        block while the body is still growing.
        """
        theme = renderer.theme
        glyph = theme.glyphs.spinner("status", self.spinner_frame)
        return f" {theme.fg('muted', glyph)}{theme.fg('dim', ' … (streaming)')}"

    def _header(self, renderer: _Renderer) -> str:
        theme = renderer.theme
        parts: list[str] = []
        if not self.streaming:
            if self.state == "error":
                icon = theme.styled_symbol("status.error", "error")
            elif self.state == "success":
                icon = theme.styled_symbol("status.success", "success")
            else:
                icon = theme.fg("accent", theme.tool_icon(self.tool))
            parts.append(icon)
        parts.append(theme.fg("toolTitle", clamp_row(self.title, TRUNCATE_LENGTHS["TITLE"])))
        line = " ".join(parts)
        if self.summary:
            summary = clamp_row(self.summary, TRUNCATE_LENGTHS["CONTENT"])
            line += f"{theme.symbol('sep.dot')}{theme.fg('muted', summary)}"
        return line

    def _body_rows(self, renderer: _Renderer, lines: Sequence[str]) -> list[str]:
        if self.language == "diff":
            return render_diff(renderer, "\n".join(lines), expanded=True, width=max(1, renderer.width - 2))
        if self.language:
            return [f" {row}" for row in renderer.code("\n".join(lines), max(1, renderer.width - 2), self.language)]
        width = max(1, renderer.width - 2)
        rows: list[str] = []
        for line in lines:
            rows.extend(f" {row}" for row in renderer.body_line(line, width))
        return rows

    def _framed(self, renderer: _Renderer, header: str, lines: Sequence[str]) -> list[str]:
        """Framed card (``render/output-block.ts:79-220``)."""
        theme = renderer.theme
        width = renderer.width
        horizontal = theme.symbol("boxRound.horizontal")
        vertical = theme.symbol("boxRound.vertical")
        cap = horizontal * 3
        border_token = _border_token(self.state)
        background = _state_background(self.state)

        left_glyphs = f"{theme.symbol('boxRound.topLeft')}{cap}"
        right_glyph = theme.symbol("boxRound.topRight")
        label = f" {header} "
        max_label = max(0, width - visible_width(left_glyphs) - visible_width(right_glyph))
        trimmed = clamp_row(label, max_label)
        fill = max(0, width - visible_width(left_glyphs) - visible_width(trimmed) - visible_width(right_glyph))
        rows = [theme.fg(border_token, left_glyphs) + trimmed + theme.fg(border_token, horizontal * fill) + theme.fg(border_token, right_glyph)]

        content_width = max(1, width - 2 - CONTENT_PADDING * 2)
        if self.language == "diff":
            body = render_diff(renderer, "\n".join(lines), expanded=True, width=content_width)
        elif self.language:
            body = renderer.code("\n".join(lines), content_width, self.language)
        else:
            body = []
            for line in lines:
                body.extend(renderer.body_line(line, content_width))
        for wrapped in body:
            row = (
                theme.fg(border_token, vertical)
                + " " * CONTENT_PADDING
                + _pad_to(wrapped, content_width)
                + " " * CONTENT_PADDING
                + theme.fg(border_token, vertical)
            )
            rows.append(theme.bg_fill(background, row))

        bottom_left = f"{theme.symbol('boxRound.bottomLeft')}{cap}"
        bottom_right = theme.symbol("boxRound.bottomRight")
        fill = max(0, width - visible_width(bottom_left) - visible_width(bottom_right))
        rows.append(theme.fg(border_token, bottom_left) + theme.fg(border_token, horizontal * fill) + theme.fg(border_token, bottom_right))
        return rows


def _border_token(state: str) -> str:
    """Border colour by state (``output-block.ts:87-94``)."""
    if state == "error":
        return "error"
    if state == "warning":
        return "warning"
    if state in ("running", "pending"):
        return "accent"
    return "dim"


def _state_background(state: str) -> str:
    """Card background by state (``render/utils.ts:105-109``)."""
    if state == "success":
        return "toolSuccessBg"
    if state == "error":
        return "toolErrorBg"
    return "toolPendingBg"


@dataclass(frozen=True, slots=True)
class _DiffLine:
    """One diff row: ``kind`` picks the render path."""

    kind: Literal["row", "raw"]
    marker: str = ""
    number: str = ""
    content: str = ""


def _diff_lines(diff: str) -> list[_DiffLine]:
    """Parse a unified diff into gutter rows, tracking old/new line numbers."""
    rows: list[_DiffLine] = []
    old_number = new_number = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            rows.append(_DiffLine("raw", content=line))
            numbers = _parse_hunk_header(line)
            if numbers is not None:
                old_number, new_number = numbers
        elif line.startswith("---") or line.startswith("+++"):
            continue
        elif line.startswith("\\"):
            rows.append(_DiffLine("raw", content=line))
        elif line.startswith("-"):
            rows.append(_DiffLine("row", marker="-", number=str(old_number), content=line[1:]))
            old_number += 1
        elif line.startswith("+"):
            rows.append(_DiffLine("row", marker="+", number=str(new_number), content=line[1:]))
            new_number += 1
        elif line.startswith(" "):
            rows.append(_DiffLine("row", marker=" ", number=str(new_number or old_number), content=line[1:]))
            old_number += 1
            new_number += 1
        else:
            rows.append(_DiffLine("raw", content=line))
    return rows


def _parse_hunk_header(line: str) -> tuple[int, int] | None:
    try:
        spec = line.split("@@")[1].strip()
        old, new = spec.split(" ")[:2]
        return int(old[1:].split(",")[0]), int(new[1:].split(",")[0])
    except IndexError, ValueError:
        return None


@dataclass
class DiffBlock(Block):
    """Unified diff rendered with omp's fixed 3-digit line-number gutter."""

    diff: str = ""
    path: str = ""

    def render(self, renderer: _Renderer) -> list[str]:
        return render_diff(renderer, self.diff, expanded=self.expanded)


def render_diff(renderer: _Renderer, diff: str, *, expanded: bool, width: int | None = None) -> list[str]:
    """Rows for a unified diff, omp's gutter grammar (``chrome/diff.ts``)."""
    theme = renderer.theme
    width = max(1, renderer.width if width is None else width)
    rows = _diff_lines(diff)
    limit = None if expanded else PREVIEW_LIMITS["DIFF_COLLAPSED_LINES"]
    hint = ""
    if limit is not None and len(rows) > limit:
        hidden = len(rows) - limit + 1
        rows = rows[: limit - 1]
        hint = renderer.hints.more_lines_hint(theme, hidden)

    line_number_width = max(3, max((len(row.number) for row in rows if row.kind == "row"), default=0))
    out: list[str] = []
    previous = ""
    for row in rows:
        if row.kind == "raw":
            out.append(clamp_row(theme.fg("toolDiffContext", replace_tabs(row.content)), width))
            previous = ""
            continue
        if row.number:
            display = "" if row.number == previous else row.number
            previous = row.number
            line = format_code_frame_line(row.marker, display, _visualize_indent(row.content), line_number_width)
        else:
            previous = ""
            line = f"{row.marker}{replace_tabs(row.content)}"
        token = "toolDiffAdded" if row.marker == "+" else "toolDiffRemoved" if row.marker == "-" else "toolDiffContext"
        out.append(clamp_row(theme.fg(token, line), width))
    if hint:
        out.append(f" {hint}")
    return out


@dataclass
class NoticeBlock(Block):
    """Informational line (dim)."""

    text: str = ""

    def render(self, renderer: _Renderer) -> list[str]:
        return [f" {renderer.dim(row)}" for row in renderer.body_line(self.text, max(1, renderer.width - 2))]


@dataclass
class ErrorBlock(Block):
    """Error line: status glyph + error colour."""

    text: str = ""

    def render(self, renderer: _Renderer) -> list[str]:
        theme = renderer.theme
        glyph = theme.styled_symbol("status.error", "error")
        rows = renderer.body_line(self.text, max(1, renderer.width - 4))
        return [f" {glyph} {theme.fg('error', row)}" for row in rows]


@dataclass
class BackgroundTaskBlock(Block):
    """Background job notice: ``icon.job`` + muted text."""

    text: str = ""

    def render(self, renderer: _Renderer) -> list[str]:
        theme = renderer.theme
        glyph = theme.styled_symbol("icon.job", "muted")
        rows = renderer.body_line(self.text, max(1, renderer.width - 4))
        return [f" {glyph} {theme.fg('muted', row)}" for row in rows]


@dataclass
class SessionMarkerBlock(Block):
    """Session/compaction divider (``compaction-summary-message.ts:39-72``)."""

    label: str = ""
    hint: str = ""

    def render(self, renderer: _Renderer) -> list[str]:
        theme = renderer.theme
        width = max(1, renderer.width)
        rule = theme.symbol("tree.horizontal")
        plain = f"{self.label} {self.hint}".rstrip()
        remaining = width - visible_width(plain) - 2
        if remaining < 4:
            return [theme.fg("muted", self.label)]
        left = remaining // 2
        right = remaining - left
        middle = f" {theme.fg('muted', self.label)} "
        if self.hint:
            middle += f"{theme.fg('dim', self.hint)} "
        return [f"{theme.fg('dim', rule * left)}{middle}{theme.fg('dim', rule * right)}"]


@dataclass
class Transcript:
    """Ordered block tape that owns the ledger of what it handed to scrollback.

    ``rows()`` renders the whole tape; ``settled_rows()`` / ``live_rows()`` split
    it at the first non-settled block so a caller can commit the settled prefix
    once and repaint only the live tail. Exactly one blank row separates
    non-empty blocks in both halves.

    The ledger marks *blocks*, not rows. :meth:`take_settled` remembers how many
    leading blocks it handed out and never renders one at or before that mark
    again. A row cursor would be the wrong ledger entry: native scrollback cannot
    be rewritten, so expanding a committed block or collapsing one would move a
    row cursor and let the next batch start inside a committed block. Marking
    blocks makes those edits structurally unable to re-emit a prefix or to
    misalign a suffix -- they change what the tape renders, never what it hands
    out.

    :meth:`set_expanded` therefore also reports whether a toggle landed behind the
    commit boundary: those rows cannot be redrawn, so the caller's only honest
    answer is to say so.
    """

    theme: Theme
    width: int
    hints: KeyHints = field(default_factory=KeyHints)
    blocks: list[Block] = field(default_factory=list)
    #: Number of leading blocks whose rows are already in native scrollback.
    _committed: int = 0
    #: ``blocks[_committed - 1]`` by identity. The tape's owner clears ``blocks``
    #: wholesale on a session switch; a ledger that no longer ends on the block
    #: it recorded must start over instead of claiming rows for blocks it no
    #: longer holds.
    _anchor: Block | None = None
    #: Rows last presented per committed block, parallel to ``blocks[:_committed]``
    #: (an empty list for a block that rendered nothing).
    _presented: list[list[str]] = field(default_factory=list)
    #: Whether any row reached scrollback yet -- decides the leading separator of
    #: the next batch and keeps a first empty batch from opening with a blank row.
    _emitted_any: bool = False

    def add(self, block: Block) -> Block:
        """Append a block and return it (mutate the returned object in place)."""
        self.blocks.append(block)
        return block

    def set_width(self, width: int) -> None:
        """Change the render width (rows re-render on the next call).

        A width change re-baselines the ledger to the committed blocks' current
        rendering: the rows in scrollback were written at the old width and cannot
        be re-derived at the new one, so treating every committed block as edited
        would reprint the whole prefix on every resize.
        """
        self.width = max(1, width)
        self._revalidate()
        self._rebaseline()

    def set_expanded(self, expanded: bool) -> bool:
        """Expand or collapse every block (``app.tools.expand``).

        Returns ``True`` when a block that is *already in scrollback* would now
        render differently: scrollback is immutable, so the caller can only report
        that honestly (it must not re-print the tape). The committed baseline is
        re-recorded either way, because expansion is a re-layout.
        """
        for block in self.blocks:
            block.expanded = expanded
        self._revalidate()
        changed = self._render_each(self.blocks[: self._committed]) != self._presented
        self._rebaseline()
        return changed

    def mark_settled(self) -> None:
        """Mark every block final and stop any streaming animation."""
        for block in self.blocks:
            block.settled = True
            if isinstance(block, ToolBlock):
                block.streaming = False
            if isinstance(block, AssistantBlock):
                block.pulse_frame = None

    def frontier(self) -> int:
        """Index of the first non-settled block (``len`` when all are final)."""
        for index, block in enumerate(self.blocks):
            if not block.settled:
                return index
        return len(self.blocks)

    def rows(self) -> list[str]:
        """Every row of the tape."""
        return self._rows_of(self.blocks, separate=False)

    def settled_rows(self) -> list[str]:
        """Rows of the settled prefix (the part that goes to scrollback)."""
        return self._rows_of(self.blocks[: self.frontier()], separate=False)

    def live_rows(self) -> list[str]:
        """Rows of the live suffix (still allowed to change)."""
        return self._rows_of(self.blocks[self.frontier() :], separate=False)

    def take_settled(self) -> list[str]:
        """Settled rows not yet handed out -- each row handed out exactly once.

        The ledger advances past every block it emitted, so a block already in
        native scrollback is never rendered for emission again, whatever later
        happens to it: a shrink or an in-place edit from :meth:`set_expanded`.
        Nothing is re-handed-out, and there is no row offset a length change could
        misalign.
        """
        self._revalidate()
        new_blocks = self.blocks[self._committed : self.frontier()]
        if not new_blocks:
            return []
        rendered = self._render_each(new_blocks)
        rows = self._join(rendered, separate=self._emitted_any)
        self._presented.extend(rendered)
        self._committed += len(new_blocks)
        self._anchor = new_blocks[-1]
        self._emitted_any = self._emitted_any or bool(rows)
        return rows

    def _rebaseline(self) -> None:
        """Re-record the committed prefix's rendering as the presented baseline."""
        self._presented = self._render_each(self.blocks[: self._committed]) if self._committed else []

    def _revalidate(self) -> None:
        """Drop a ledger whose block list was replaced out from under it."""
        if self._committed > len(self.blocks) or (self._committed > 0 and self.blocks[self._committed - 1] is not self._anchor):
            self._reset_ledger()

    def _reset_ledger(self) -> None:
        self._committed = 0
        self._anchor = None
        self._presented = []
        self._emitted_any = False

    def _render_each(self, blocks: Sequence[Block]) -> list[list[str]]:
        renderer = _Renderer(self.theme, self.width, self.hints)
        return [_trim_blank_edges(block.render(renderer)) for block in blocks]

    def _rows_of(self, blocks: Sequence[Block], *, separate: bool) -> list[str]:
        return self._join(self._render_each(blocks), separate=separate)

    @staticmethod
    def _join(rendered: Sequence[Sequence[str]], *, separate: bool) -> list[str]:
        """One row list from per-block rows: blank edges trimmed, single separators."""
        rows: list[str] = []
        for block_rows in rendered:
            if not block_rows:
                continue
            if rows or separate:
                rows.append("")
            rows.extend(block_rows)
        return rows


def _trim_blank_edges(rows: Sequence[str]) -> list[str]:
    """Strip leading/trailing blank rows (``transcript-container.ts:134-138``)."""
    start = 0
    end = len(rows)
    while start < end and _is_plain_blank(rows[start]):
        start += 1
    while end > start and _is_plain_blank(rows[end - 1]):
        end -= 1
    return list(rows[start:end])


def _is_plain_blank(row: str) -> bool:
    """A row with no visible text counts as blank (``transcript-container.ts:117``)."""
    return visible_width(row.strip()) == 0
