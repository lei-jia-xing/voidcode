"""One-line status bar: omp's default segment set, restricted to real data.

Ported from oh-my-pi (MIT; ``/tmp/omp-src``):

* Segment catalog: ``packages/tui/src/status-line/schema.ts:1-22``.
* ``default`` preset (segment order, separator, per-segment options):
  ``status-line/presets.ts:6-18``.
* Separator glyphs (``powerline-thin`` = ``>``/``<`` with background-coloured
  end caps): ``status-line/separators.ts:8-25``.
* Group assembly, end caps, gap gauge, and the overflow order (shrink session
  name, pop right segments, shrink path, then drop left segments -- never the
  path first): ``status-line/component.ts:2700-2820``.
* Segment bodies: ``status-line/segments.ts`` (``status`` ``:197``, ``model``
  ``:213``, ``mode`` ``:364``, ``path`` ``:413``, ``cost`` ``:565``,
  ``context_pct`` ``:603``, ``session_name`` ``:740``).
* Format helpers: ``formatContextUsage``/``getContextUsageLevel``/
  ``getContextUsageThemeColor`` (``chrome/context-thresholds.ts:30-110``),
  ``formatEmbeddedContextPercent``/``embeddedContextGaugeMinWidth``
  (``status-line/component.ts:419-425``), ``formatNumber``
  (``packages/utils/src/format.ts:34-48``), ``shortenPath``/``clampPathLength``
  (``render/render-utils.ts:902-916``, ``status-line/segments.ts:62-66``).

Omitted, with reason (see the module's reply notes): the ``pi``/``vim``/
``collab``/``stream``/``git``/``pr`` segments (no data in voidcode), the
session-accent colour hash, compaction/speculation boundary markers (no
speculation state in the runtime), subscription/premium-request billing, and the
``hostname``/``time``/``token_*``/``cache_*``/``usage`` segments (not in omp's
default preset).

Pure: renders one row, writes nothing.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from .term import clamp_row, format_number, visible_width
from .theme import Theme

__all__ = [
    "STATUS_LINE_DEFAULT_PRESET",
    "STATUS_LINE_SEGMENT_IDS",
    "StatusLine",
    "StatusLinePreset",
    "StatusSegmentData",
    "format_context_usage",
    "format_number",
]

#: ``STATUS_LINE_SEGMENT_IDS`` (``status-line/schema.ts:1-30``).
STATUS_LINE_SEGMENT_IDS: Final[tuple[str, ...]] = (
    "pi",
    "status",
    "model",
    "mode",
    "path",
    "git",
    "pr",
    "subagents",
    "token_in",
    "token_out",
    "token_total",
    "token_rate",
    "cost",
    "context_pct",
    "context_total",
    "time_spent",
    "time",
    "session",
    "hostname",
    "cache_read",
    "cache_write",
    "cache_hit",
    "session_name",
    "usage",
    "collab",
    "stream",
    "vim",
)

#: ``TRUNCATE_LENGTHS`` subset used by the status line.
_PATH_MAX_LENGTH: Final = 40
_PATH_MIN_WIDTH: Final = 8
_NAME_MIN_WIDTH: Final = 8

_CONTEXT_WARNING_PERCENT: Final = 50
_CONTEXT_WARNING_TOKENS: Final = 150_000
_CONTEXT_PURPLE_PERCENT: Final = 70
_CONTEXT_PURPLE_TOKENS: Final = 270_000
_CONTEXT_ERROR_PERCENT: Final = 90
_CONTEXT_ERROR_TOKENS: Final = 500_000


#: ``STATUS_LINE_PRESETS.default`` (``status-line/presets.ts:6-18``), restricted
#: to the segments voidcode has data for. omp's leading ``pi`` (brand + working
#: timer) and ``vim`` describe omp-only features; voidcode's equivalent leading
#: datum is its turn state, so the ``status`` catalog id leads instead. The
#: tail order (``model``, ``mode``, ``path``, ``context_pct``, ``cost`` /
#: ``session_name``) is omp's, unchanged.
@dataclass(frozen=True, slots=True)
class StatusLinePreset:
    """A named set of segments plus its separator style."""

    left: tuple[str, ...]
    right: tuple[str, ...]
    separator: str = "powerline-thin"


STATUS_LINE_DEFAULT_PRESET: Final[StatusLinePreset] = StatusLinePreset(
    left=("status", "model", "mode", "path", "context_pct", "cost"),
    # ``lsp`` trails ``session_name``: it is voidcode's own datum (no omp
    # counterpart) and the right group is what overflow pops first.
    right=("session_name", "lsp"),
    separator="powerline-thin",
)


def format_context_usage(percent: float | None, window: int, tokens: int = 0) -> str:
    """``42.5%/200K`` or ``12K/?`` when the window is unknown (``context-thresholds.ts:62-71``)."""
    if window <= 0:
        return f"{format_number(tokens)}/?"
    label = "?" if percent is None else f"{percent:.1f}%"
    return f"{label}/{format_number(window)}"


def _format_embedded_percent(percent: float) -> str:
    """``1%/42%`` with one decimal only below 1 (``component.ts:419-421``)."""
    return f"{percent:.1f}%" if 0 < percent < 1 else f"{round(percent)}%"


def _context_level(percent: float, window: int) -> str:
    """Context usage band (``context-thresholds.ts:30-58``)."""

    def reaches(percent_threshold: float, token_threshold: int) -> bool:
        if percent <= 0:
            return False
        if window <= 0:
            return percent >= percent_threshold
        return percent >= min(percent_threshold, (token_threshold / window) * 100)

    if reaches(_CONTEXT_ERROR_PERCENT, _CONTEXT_ERROR_TOKENS):
        return "error"
    if reaches(_CONTEXT_PURPLE_PERCENT, _CONTEXT_PURPLE_TOKENS):
        return "purple"
    if reaches(_CONTEXT_WARNING_PERCENT, _CONTEXT_WARNING_TOKENS):
        return "warning"
    return "normal"


def _context_color(level: str) -> str:
    """``getContextUsageThemeColor`` (``context-thresholds.ts:74-85``)."""
    if level == "error":
        return "error"
    if level == "purple":
        return "thinkingHigh"
    if level == "warning":
        return "warning"
    return "statusLineContext"


def shorten_path(path: str, home: str | None = None) -> str:
    """Replace a leading home directory with ``~`` (``render-utils.ts:902-916``)."""
    resolved_home = home if home is not None else os.path.expanduser("~")
    if resolved_home and resolved_home != "/" and path.startswith(resolved_home):
        suffix = path[len(resolved_home) :]
        if suffix == "" or suffix.startswith(os.sep):
            return f"~{suffix}"
    return path


def clamp_path_length(path: str, max_length: int) -> str:
    """Keep the tail of a long path (``segments.ts:62-66``)."""
    if len(path) <= max_length:
        return path
    return f"…{path[-max(0, max_length - 1) :]}"


def _abbreviate(path: str, home: str | None = None) -> str:
    """``shortenPath`` + ``stripDisplayRoot``-style leading-root drop."""
    shortened = shorten_path(path, home)
    if shortened.startswith("~/") or shortened.startswith("~"):
        return shortened
    parts = shortened.split(os.sep)
    if len(parts) > 2 and parts[0] == "":
        # Drop a single leading root/anchor segment so the tail stays readable.
        return os.sep.join(parts[1:])
    return shortened


def _with_icon(icon: str, text: str) -> str:
    """``withIcon`` (``segments.ts:33-35``)."""
    return f"{icon} {text}" if icon else text


@dataclass(frozen=True, slots=True)
class StatusSegmentData:
    """The data the status line renders. Empty values hide their segment."""

    state: str = ""
    model: str = ""
    #: Canonical reasoning effort (``provider/reasoning_effort.py``): one of
    #: ``off|minimal|low|medium|high|xhigh|max``, matching the ``thinking.*`` glyphs.
    thinking: str = "off"
    mode: str = ""
    path: str = ""
    #: Language-server summary (``lsp 2`` / ``lsp idle`` / ``lsp off``). voidcode's
    #: own datum: omp has no LSP segment, and the runtime exposes no per-turn LSP
    #: event, so the client renders ``current_lsp_state()`` here instead of losing
    #: the old sidebar's fifth panel.
    lsp: str = ""
    session_name: str = ""
    cost_usd: float = 0.0
    context_percent: float | None = None
    context_window: int = 0
    context_tokens: int = 0


@dataclass(frozen=True, slots=True)
class _Part:
    """A rendered segment plus the id it came from."""

    segment: str
    content: str


class StatusLine:
    """Renders omp's default status line at a fixed width."""

    __slots__ = ("_preset", "_theme", "_width")

    def __init__(self, theme: Theme, width: int) -> None:
        self._theme = theme
        self._width = max(1, width)
        self._preset = STATUS_LINE_DEFAULT_PRESET

    def set_width(self, width: int) -> None:
        """Resize the bar (rows are re-rendered next call)."""
        self._width = max(1, width)

    # -- segments ----------------------------------------------------------

    def _status(self, data: StatusSegmentData) -> str:
        """``statusSegment`` (``segments.ts:197-211``) -- accent, `` · ``-joined."""
        if not data.state:
            return ""
        return self._theme.fg("accent", data.state)

    def _model(self, data: StatusSegmentData) -> str:
        """``modelSegment`` (``segments.ts:213-300``), thinking tail included."""
        if not data.model:
            return ""
        theme = self._theme
        name = data.model[7:] if data.model.startswith("Claude ") else data.model
        content = theme.fg("statusLineModel", _with_icon(theme.symbol("icon.model"), name))
        thinking = self._thinking_display(data)
        if thinking:
            content += theme.fg("statusLineModel", f"{theme.symbol('sep.dot')}{thinking}")
        return content

    def _thinking_display(self, data: StatusSegmentData) -> str:
        """Thinking-level display (``segments.ts:230-247``)."""
        if data.thinking in ("", "off"):
            return f"{self._theme.symbol('status.disabled')} off"
        return self._theme.glyphs.thinking(data.thinking) or data.thinking

    def _mode(self, data: StatusSegmentData) -> str:
        """``modeSegment`` (``segments.ts:364-411``) -- bare accent label here.

        omp's mode icons are per-mode (plan/prewalk/vibe/loop); voidcode's mode is
        the approval mode, which has no omp counterpart icon, so the label is
        rendered in the same accent colour without one.
        """
        if not data.mode:
            return ""
        return self._theme.fg("accent", data.mode)

    def _path(self, data: StatusSegmentData, max_length: int = _PATH_MAX_LENGTH) -> str:
        """``pathSegment`` (``segments.ts:413-457``)."""
        if not data.path:
            return ""
        theme = self._theme
        text = clamp_path_length(_abbreviate(data.path), max_length)
        return theme.fg("statusLinePath", _with_icon(theme.symbol("icon.folder"), text))

    def _context_pct(self, data: StatusSegmentData) -> str:
        """``contextPctSegment`` (``segments.ts:603-634``)."""
        if data.context_percent is None and not data.context_window and not data.context_tokens:
            return ""
        theme = self._theme
        percent = data.context_percent or 0.0
        color = _context_color(_context_level(percent, data.context_window))
        text = theme.fg(color, format_context_usage(data.context_percent, data.context_window, data.context_tokens))
        return _with_icon(theme.symbol("icon.context"), text)

    def _cost(self, data: StatusSegmentData) -> str:
        """``costSegment`` (``segments.ts:565-599``) -- hidden at zero spend."""
        if not data.cost_usd:
            return ""
        return self._theme.fg("statusLineCost", f"${data.cost_usd:.2f}")

    def _lsp(self, data: StatusSegmentData) -> str:
        """Language-server status -- hidden when the client has no LSP data."""
        if not data.lsp:
            return ""
        return self._theme.fg("muted", data.lsp)

    def _session_name(self, data: StatusSegmentData) -> str:
        """``sessionNameSegment`` (``segments.ts:740-749``)."""
        if not data.session_name:
            return ""
        return self._theme.fg("accent", data.session_name)

    # -- layout ------------------------------------------------------------

    def render(self, data: StatusSegmentData) -> str:
        """Render the status line, or ``""`` when nothing has data."""
        theme = self._theme
        width = self._width
        if width <= 0:
            return ""
        left_ids = self._preset.left
        right_ids = self._preset.right
        left = _parts(self, data, left_ids)
        right = _parts(self, data, right_ids)
        if not left and not right:
            return ""

        # The preset's only separator style (``status-line/separators.ts:8-25``):
        # thin powerline glyphs with background-coloured end caps.
        sep_left = theme.symbol("sep.powerlineThinLeft")
        sep_right = theme.symbol("sep.powerlineThinRight")
        cap_left = theme.symbol("sep.powerlineLeft")
        cap_right = theme.symbol("sep.powerlineRight")

        left_cap_width = visible_width(cap_right)
        right_cap_width = visible_width(cap_left)

        def group_width(parts: Sequence[_Part], cap: int, sep: str) -> int:
            if not parts:
                return 0
            sep_width = visible_width(sep) + 2
            return sum(visible_width(part.content) for part in parts) + (len(parts) - 1) * sep_width + 2 + cap

        left_width = group_width(left, left_cap_width, sep_left)
        right_width = group_width(right, right_cap_width, sep_right)
        gauge_min = _embedded_gauge_min_width(data)

        def total() -> int:
            gap = gauge_min if gauge_min else (1 if left and right else 0)
            return left_width + right_width + gap

        # Overflow order (``component.ts:2716-2783``): shrink the session name,
        # then pop right segments, then shrink the path, then drop left segments
        # with the path last.
        if total() > width and right:
            name_index = _index_of(right, "session_name")
            if name_index is not None and total() > width:
                current = visible_width(right[name_index].content)
                shrink = min(
                    max(0, current - _NAME_MIN_WIDTH),
                    total() - width,
                )
                if shrink > 0:
                    right[name_index] = replace(right[name_index], content=clamp_row(right[name_index].content, current - shrink))
                    right_width = group_width(right, right_cap_width, sep_right)
        while total() > width and right:
            right.pop()
            right_width = group_width(right, right_cap_width, sep_right)
        if total() > width and left:
            path_index = _index_of(left, "path")
            if path_index is not None:
                current = visible_width(left[path_index].content)
                shrink = min(max(0, current - _PATH_MIN_WIDTH), total() - width)
                if shrink > 0:
                    max_length = max(4, min(_PATH_MAX_LENGTH, current) - shrink)
                    for _ in range(8):
                        rendered = self._path(data, max_length)
                        if current - visible_width(rendered) >= shrink:
                            break
                        if max_length <= 4:
                            break
                        max_length = max(4, max_length - 1)
                    left[path_index] = replace(left[path_index], content=rendered)
                    left_width = group_width(left, left_cap_width, sep_left)
        while total() > width and left:
            drop = _left_drop_index(left)
            if drop is None:
                break
            left.pop(drop)
            left_width = group_width(left, left_cap_width, sep_left)

        left_group = self._render_group(left, "left", sep_left, cap_right)
        right_group = self._render_group(right, "right", sep_right, cap_left)
        if not left_group and not right_group:
            return ""
        # The gap always exists: with one group absent the gauge still runs from
        # (or up to) the border edge (``component.ts:2810-2820``), which also
        # keeps the width accounting above honest.
        gap_width = max(1, width - left_width - right_width)
        return clamp_row(left_group + self._gauge(gap_width, data) + right_group, width)

    def _render_group(self, parts: Sequence[_Part], direction: str, separator: str, cap: str) -> str:
        """``renderGroup`` (``status-line/component.ts:2770-2795``)."""
        if not parts:
            return ""
        theme = self._theme
        background = theme.get_bg_ansi("statusLineBg")
        foreground = theme.get_fg_ansi("text")
        sep_ansi = theme.get_fg_ansi("statusLineSep")
        # End caps are drawn in the bar's own background colour so they bridge
        # the band into the surrounding terminal (``useBgAsFg``).
        cap_prefix = background.replace("\x1b[48;", "\x1b[38;", 1)
        cap_text = f"{cap_prefix}{cap}\x1b[0m" if cap else ""
        content = background + foreground
        content += f" {f' {sep_ansi}{separator}{foreground} '.join(part.content for part in parts)} "
        content += "\x1b[0m"
        if direction == "right":
            return cap_text + content
        return content + cap_text

    def _gauge(self, gap_width: int, data: StatusSegmentData) -> str:
        """Context gauge bridging the groups (``component.ts:2806-2935``).

        ``embedded`` mode: the used portion in ``borderAccent``, the rest in
        ``border``, with the percentage and window labels absorbed at the right
        end. omp's session-accent colouring and compaction/speculation boundary
        markers are omitted (no speculation state here), so the used portion uses
        the documented ``borderAccent`` fallback.
        """
        theme = self._theme
        horizontal = theme.symbol("boxRound.horizontal")
        percent = data.context_percent
        if percent is None or not data.context_window:
            return f"\x1b[49m{theme.get_fg_ansi('borderAccent')}{horizontal * gap_width}\x1b[39m"

        clamped = min(100.0, max(0.0, percent))
        percent_label = _format_embedded_percent(percent if percent > 100 else clamped)
        window_label = format_number(data.context_window)
        minimum = len(percent_label) + len(window_label) + 4
        percent_start = window_start = -1
        scale_width = gap_width
        if gap_width >= minimum:
            if percent > 100:
                percent_start = gap_width - len(percent_label)
                window_start = percent_start - 1 - len(window_label)
            else:
                window_start = gap_width - len(window_label) - 1
            scale_width = max(1, window_start)
        used_count = min(scale_width, max(1, round((clamped / 100) * scale_width)))
        if percent_label and percent_start < 0:
            max_start = scale_width - len(percent_label) - 1
            percent_start = min(max_start, max(1, used_count))

        used_color = theme.get_fg_ansi("borderAccent")
        unused_color = theme.get_fg_ansi("border")
        overflow_color = theme.get_fg_ansi("error")
        out = "\x1b[49m"
        active = ""
        for index in range(gap_width):
            color = used_color if index < used_count else unused_color
            glyph = horizontal
            if percent_start >= 0 and percent_start <= index < percent_start + len(percent_label):
                color = overflow_color if percent > 100 else used_color
                glyph = percent_label[index - percent_start]
            elif window_start >= 0 and window_start <= index < window_start + len(window_label):
                color = used_color
                glyph = window_label[index - window_start]
            if color != active:
                out += color
                active = color
            out += glyph
        return out


def _parts(line: StatusLine, data: StatusSegmentData, ids: Sequence[str]) -> list[_Part]:
    """Render the visible segments of one group, in order."""
    renderers = {
        "status": line._status,
        "model": line._model,
        "mode": line._mode,
        "path": line._path,
        "context_pct": line._context_pct,
        "cost": line._cost,
        "lsp": line._lsp,
        "session_name": line._session_name,
    }
    parts: list[_Part] = []
    for segment in ids:
        render = renderers.get(segment)
        if render is None:
            continue
        content = render(data)
        if content:
            parts.append(_Part(segment, content))
    return parts


def _index_of(parts: Sequence[_Part], segment: str) -> int | None:
    for index, part in enumerate(parts):
        if part.segment == segment:
            return index
    return None


def _left_drop_index(parts: Sequence[_Part]) -> int | None:
    """Index of the next left segment to drop, keeping the path (``component.ts:2743-2752``)."""
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].segment != "path":
            return index
    return len(parts) - 1 if parts else None


def _embedded_gauge_min_width(data: StatusSegmentData) -> int:
    """``embeddedContextGaugeMinWidth`` (``component.ts:423-425``)."""
    if data.context_percent is None or not data.context_window:
        return 0
    return len(_format_embedded_percent(min(100.0, max(0.0, data.context_percent)))) + len(format_number(data.context_window)) + 4
