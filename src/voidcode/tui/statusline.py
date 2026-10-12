"""Configured, one-row TUI statusline rendering."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from ..runtime.config_models import (
    VALID_TUI_STATUSLINE_SEGMENTS,
    TuiStatuslineLayout,
    TuiStatuslineSegment,
    TuiStatuslineSeparator,
)
from .term import clamp_row, crop_row_right, escape_terminal_controls, format_number, visible_width
from .theme import Theme

if TYPE_CHECKING:
    from ..runtime.config import RuntimeTuiStatuslineConfig

__all__ = [
    "STATUS_LINE_SEGMENT_IDS",
    "StatusLine",
    "StatusSegmentData",
    "format_context_usage",
    "statusline_profile_segments",
    "statusline_profiles",
]

STATUS_LINE_SEGMENT_IDS: Final = VALID_TUI_STATUSLINE_SEGMENTS

_CONTEXT_WARNING_PERCENT: Final = 50
_CONTEXT_WARNING_TOKENS: Final = 150_000
_CONTEXT_PURPLE_PERCENT: Final = 70
_CONTEXT_PURPLE_TOKENS: Final = 270_000
_CONTEXT_ERROR_PERCENT: Final = 90
_CONTEXT_ERROR_TOKENS: Final = 500_000

_PROFILES: Final[
    Mapping[
        TuiStatuslineLayout,
        tuple[tuple[TuiStatuslineSegment, ...], tuple[TuiStatuslineSegment, ...], TuiStatuslineSeparator],
    ]
] = {
    "codex": (("status", "model", "mode", "path"), ("context_pct", "cost"), "dot"),
    "claude": (("model", "path"), ("mode", "context_pct", "cost", "session_name"), "rule"),
    "minimal": (("status", "model", "mode"), (), "none"),
    "compact": (("status", "model", "mode", "path"), ("context_pct", "cost"), "pipe"),
    "full": (
        ("status", "model", "mode", "path", "session_name"),
        ("context_pct", "cost", "token_in", "token_out", "cache_read", "cache_write", "cache_hit"),
        "dot",
    ),
    "ascii": (("status", "model", "mode", "path"), ("context_pct", "cost"), "pipe"),
    "custom": ((), (), "dot"),
}


def statusline_profile_segments(
    profile: TuiStatuslineLayout,
) -> tuple[tuple[TuiStatuslineSegment, ...], tuple[TuiStatuslineSegment, ...]]:
    """Return the renderer's authoritative left/right segment IDs for a profile."""
    left, right, _separator = _PROFILES[profile]
    return left, right


def statusline_profiles() -> tuple[TuiStatuslineLayout, ...]:
    """Return the renderer's authoritative layout IDs, including ``custom``."""
    return tuple(_PROFILES)


_DROP_PRIORITY: Final[Mapping[str, int]] = {
    "cache_hit": 0,
    "cache_write": 1,
    "cache_read": 2,
    "token_out": 3,
    "token_in": 4,
    "cost": 5,
    "lsp": 6,
    "session_name": 7,
    "path": 8,
    "context_pct": 9,
    "model": 10,
    "status": 11,
    "mode": 12,
}

_DEFAULT_COLORS: Final[Mapping[str, str]] = {
    "status": "accent",
    "model": "statusLineModel",
    "mode": "accent",
    "path": "statusLinePath",
    "context_pct": "statusLineContext",
    "cost": "statusLineCost",
    "session_name": "accent",
    "lsp": "muted",
    "token_in": "statusLineContext",
    "token_out": "statusLineContext",
    "cache_read": "muted",
    "cache_write": "statusLineContext",
    "cache_hit": "muted",
}

_ICON_SYMBOLS: Final[Mapping[str, str]] = {
    "status": "statusline.status",
    "model": "icon.model",
    "mode": "statusline.mode",
    "path": "icon.folder",
    "context_pct": "icon.context",
    "cost": "statusline.cost",
    "session_name": "statusline.session",
    "lsp": "icon.job",
    "token_in": "statusline.tokenIn",
    "token_out": "statusline.tokenOut",
    "cache_read": "statusline.cacheRead",
    "cache_write": "statusline.cacheWrite",
    "cache_hit": "statusline.cacheHit",
}

_DEFAULT_ICONS: Final = frozenset({"model", "path", "context_pct", "lsp"})

_DROPPABLE: Final = frozenset(_DROP_PRIORITY) - {"status", "mode"}


def format_context_usage(percent: float | None, window: int, tokens: int = 0) -> str:
    """Format known context usage without inventing a percentage."""
    if window <= 0:
        return f"{format_number(tokens)}/?"
    label = "?" if percent is None else f"{percent:.1f}%"
    return f"{label}/{format_number(window)}"


def _context_level(percent: float, window: int) -> str:
    def reaches(percent_threshold: float, token_threshold: int) -> bool:
        if percent <= 0:
            return False
        threshold = percent_threshold if window <= 0 else min(percent_threshold, token_threshold / window * 100)
        return percent >= threshold

    if reaches(_CONTEXT_ERROR_PERCENT, _CONTEXT_ERROR_TOKENS):
        return "error"
    if reaches(_CONTEXT_PURPLE_PERCENT, _CONTEXT_PURPLE_TOKENS):
        return "purple"
    if reaches(_CONTEXT_WARNING_PERCENT, _CONTEXT_WARNING_TOKENS):
        return "warning"
    return "normal"


def _context_color(level: str) -> str:
    return {
        "error": "error",
        "purple": "thinkingHigh",
        "warning": "warning",
    }.get(level, "statusLineContext")


def _safe(text: str) -> str:
    return escape_terminal_controls(text)


def _truncate(text: str, width: int, marker: str = "...") -> str:
    if width <= 0:
        return ""
    if visible_width(text) <= width:
        return text
    marker = marker if visible_width(marker) <= width else clamp_row(marker, width)
    return clamp_row(text, width - visible_width(marker)) + marker


def shorten_path(path: str, home: str | None = None) -> str:
    """Replace a leading home directory with ``~``."""
    resolved_home = home if home is not None else os.path.expanduser("~")
    if resolved_home and resolved_home != "/" and path.startswith(resolved_home):
        suffix = path[len(resolved_home) :]
        if suffix == "" or suffix.startswith(os.sep):
            return f"~{suffix}"
    return path


def _path_text(path: str) -> str:
    shortened = shorten_path(_safe(path))
    if shortened.startswith(("~", os.sep)):
        return shortened
    parts = shortened.split(os.sep)
    return os.sep.join(parts[1:]) if len(parts) > 2 and parts[0] == "" else shortened


def _with_icon(icon: str, text: str) -> str:
    return f"{icon} {text}" if icon else text


@dataclass(frozen=True, slots=True)
class StatusSegmentData:
    """Latest runtime data consumed by statusline segments."""

    state: str = ""
    model: str = ""
    thinking: str = "off"
    mode: str = ""
    path: str = ""
    lsp: str = ""
    session_name: str = ""
    cost_usd: float | None = None
    context_percent: float | None = None
    context_window: int = 0
    context_tokens: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_hit_rate: float | None = None


@dataclass(slots=True)
class _Part:
    segment: str
    text: str
    color: str
    max_width: int | None
    content: str


class StatusLine:
    """Resolve one configured layout into a single width-bounded row."""

    __slots__ = ("_theme", "_width", "_config", "_left_ids", "_right_ids", "_separator")

    def __init__(self, theme: Theme, width: int, config: RuntimeTuiStatuslineConfig) -> None:
        self._theme = theme
        self._width = max(1, width)
        self._config = config
        default_left, default_right, default_separator = _PROFILES[config.layout]
        self._left_ids = config.left if config.left is not None else default_left
        self._right_ids = config.right if config.right is not None else default_right
        self._separator = config.separator or default_separator

    def set_width(self, width: int) -> None:
        self._width = max(1, width)

    def _symbol(self, key: str) -> str:
        if self._config.layout == "ascii":
            return self._theme.symbol(key, preset="ascii")
        return self._theme.symbol(key)

    def _icon(self, segment: str) -> str:
        option = self._config.options.get(segment)
        enabled = option.icon if option is not None and option.icon is not None else segment in _DEFAULT_ICONS
        key = _ICON_SYMBOLS.get(segment)
        return self._symbol(key) if enabled and key is not None else ""

    def _text(self, segment: str, data: StatusSegmentData) -> str:
        if segment == "status":
            state = _safe(data.state)
            return "" if state.casefold() == "idle" else state
        if segment == "model":
            if not data.model:
                return ""
            model = _safe(data.model)
            thinking = _safe(data.thinking)
            if thinking and thinking.casefold() != "off":
                marker = self._symbol(f"thinking.{thinking.casefold()}") or thinking
                model += f"{self._symbol('sep.dot')}{marker}"
            return model
        if segment == "mode":
            return _safe(data.mode)
        if segment == "path":
            return _path_text(data.path) if data.path else ""
        if segment == "context_pct":
            if data.context_percent is None and not data.context_window and not data.context_tokens:
                return ""
            return format_context_usage(data.context_percent, data.context_window, data.context_tokens)
        if segment == "cost":
            return "" if data.cost_usd is None else f"${data.cost_usd:.2f}"
        if segment == "session_name":
            return _safe(data.session_name)
        if segment == "lsp":
            label = _safe(data.lsp)
            normalized = " ".join(label.split()).casefold()
            return "" if normalized in {"off", "idle", "disabled", "lsp off", "lsp idle", "lsp disabled"} else label
        if segment == "token_in":
            return "" if data.input_tokens is None else f"in {format_number(data.input_tokens)}"
        if segment == "token_out":
            return "" if data.output_tokens is None else f"out {format_number(data.output_tokens)}"
        if segment == "cache_read":
            return "" if data.cache_read_tokens is None else f"cache read {format_number(data.cache_read_tokens)}"
        if segment == "cache_write":
            return "" if data.cache_write_tokens is None else f"cache write {format_number(data.cache_write_tokens)}"
        if segment == "cache_hit":
            return "" if data.cache_hit_rate is None else f"cache hit {data.cache_hit_rate * 100:.1f}%"
        raise AssertionError(f"Unregistered statusline segment: {segment}")

    def _part(self, segment: str, data: StatusSegmentData) -> _Part | None:
        text = self._text(segment, data)
        if not text:
            return None
        text = _with_icon(self._icon(segment), text)
        color = _DEFAULT_COLORS[segment]
        if segment == "context_pct":
            color = _context_color(_context_level(data.context_percent or 0.0, data.context_window))
        option = self._config.options.get(segment)
        if option is not None and option.color is not None:
            color = option.color
        max_width = option.max_width if option is not None else None
        content = self._theme.fg(color, text)
        if max_width is not None:
            content = clamp_row(content, max_width)
        return _Part(segment, text, color, max_width, content)

    def _parts(self, data: StatusSegmentData, ids: Sequence[str]) -> list[_Part]:
        return [part for segment in ids if (part := self._part(segment, data)) is not None]

    def _style(self, part: _Part) -> None:
        part.content = self._theme.fg(part.color, part.text)
        if part.max_width is not None:
            part.content = clamp_row(part.content, part.max_width)

    def _crop_path(self, part: _Part, width: int) -> None:
        marker = self._truncate("...", width)
        suffix = crop_row_right(part.text, max(0, width - visible_width(marker)))
        part.text = marker + suffix
        self._style(part)

    def _truncate(self, text: str, width: int) -> str:
        return _truncate(text, width, "...")

    def _separator_text(self) -> str:
        if self._separator == "none":
            return " "
        if self._separator == "dot":
            return self._symbol("sep.dot")
        if self._separator == "pipe":
            return " | "
        if self._separator == "rule":
            glyph = self._symbol("sep.rule")
            return f" {glyph} " if glyph else " "
        return " "

    def _joined(self, parts: Sequence[_Part], separator: str) -> str:
        if not parts:
            return ""
        styled_sep = self._theme.fg("statusLineSep", separator) if separator.strip() else separator
        return styled_sep.join(part.content for part in parts)

    def _total_width(self, left: Sequence[_Part], right: Sequence[_Part], separator: str) -> int:
        sep_width = visible_width(separator)
        left_width = sum(visible_width(p.content) for p in left) + max(0, len(left) - 1) * sep_width
        right_width = sum(visible_width(p.content) for p in right) + max(0, len(right) - 1) * sep_width
        return left_width + right_width + (1 if left and right else 0)

    def _fit(self, left: list[_Part], right: list[_Part], separator: str) -> None:
        width = self._width

        def total() -> int:
            return self._total_width(left, right, separator)

        for part in (*left, *right):
            if part.segment == "path" and visible_width(part.content) > 40:
                self._crop_path(part, 40)

        for part in (*left, *right):
            if part.segment == "path" and total() > width:
                current = visible_width(part.content)
                self._crop_path(part, min(current, max(8, current - (total() - width))))
        for segment in sorted(_DROPPABLE, key=_DROP_PRIORITY.__getitem__):
            if total() <= width:
                break
            left[:] = [part for part in left if part.segment != segment]
            right[:] = [part for part in right if part.segment != segment]
        for segment in ("status", "model", "mode"):
            for part in (*left, *right):
                if part.segment == segment and total() > width:
                    part.text = self._truncate(part.text, max(1, visible_width(part.content) - (total() - width)))
                    self._style(part)
        while total() > width and (left or right):
            candidates = [(part, group) for group in (right, left) for part in group if part.segment not in {"status", "mode"}]
            if not candidates:
                break
            part, group = min(candidates, key=lambda item: _DROP_PRIORITY[item[0].segment])
            group.remove(part)

    def render(self, data: StatusSegmentData) -> str:
        separator = self._separator_text()
        left = self._parts(data, self._left_ids)
        right = self._parts(data, self._right_ids)
        if not left and not right:
            return ""
        self._fit(left, right, separator)
        left_text = self._joined(left, separator)
        right_text = self._joined(right, separator)
        left_width = visible_width(left_text)
        right_width = visible_width(right_text)
        if left and right:
            row = left_text + " " * max(1, self._width - left_width - right_width) + right_text
        elif right:
            row = " " * max(0, self._width - right_width) + right_text
        else:
            row = left_text
        if not self._config.transparent:
            row += " " * max(0, self._width - visible_width(row))
            row = self._theme.bg("statusLineBg", row)
        elif self._theme.color_system is not None:
            row = f"\x1b[49m{row}\x1b[49m"
        return clamp_row(row, self._width)
