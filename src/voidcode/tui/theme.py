"""Theme tokens, palettes, and glyph tables.

Pure theme resolution and styling primitives for the TUI.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from re import compile as _compile
from types import MappingProxyType
from typing import Final, Literal, get_args

from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Token,
)
from rich.color import Color, ColorSystem, ColorType
from rich.style import Style
from rich.syntax import SyntaxTheme
from rich.theme import Theme as RichTheme

#: The renderer foreground vocabulary (omp ``ThemeColor``, ``theme/schema.ts``),
#: declared here: the core config has no theme section, so this module owns
#: the token names its palettes must define.
type _ForegroundToken = Literal[
    "accent",
    "border",
    "borderAccent",
    "success",
    "error",
    "warning",
    "muted",
    "dim",
    "text",
    "thinkingText",
    "userMessageText",
    "toolTitle",
    "mdHeading",
    "mdLink",
    "mdLinkUrl",
    "mdCode",
    "mdCodeBlock",
    "mdQuote",
    "mdHr",
    "mdListBullet",
    "toolDiffAdded",
    "toolDiffRemoved",
    "toolDiffContext",
    "syntaxComment",
    "syntaxKeyword",
    "syntaxFunction",
    "syntaxVariable",
    "syntaxString",
    "syntaxNumber",
    "syntaxOperator",
    "syntaxPunctuation",
    "thinkingHigh",
    "statusLineSep",
    "statusLineModel",
    "statusLinePath",
    "statusLineContext",
    "statusLineCost",
]


def _contains_terminal_controls(value: str) -> bool:
    """C0/C1 bytes must never reach the terminal as commands."""
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)


__all__ = [
    "BG_TOKENS",
    "DEFAULT_GLYPH_PRESET",
    "DEFAULT_THEME_NAMES",
    "FG_TOKENS",
    "SPINNER_FRAMES",
    "THEME_NAMES",
    "SYMBOL_PRESETS",
    "palette_names",
    "resolve_theme",
]

#: Every palette the registry knows: the pinned catalog below plus the VoidCode palettes.
#: Exact upstream identities are loaded from the shipped pinned catalog.
_RESOURCE_ROOT = files(__package__).joinpath("themes")
_CATALOG = json.loads(_RESOURCE_ROOT.joinpath("index.json").read_text(encoding="utf-8"))
_UPSTREAM_THEMES: Final[Mapping[str, Mapping[str, str]]] = _CATALOG["themes"]
_VOIDCODE_THEMES: Final[Mapping[str, str]] = MappingProxyType({"voidcode-dark": "dark", "voidcode-light": "light", "voidcode-high-contrast": "dark"})
THEME_NAMES: Final[frozenset[str]] = frozenset((*_UPSTREAM_THEMES, *_VOIDCODE_THEMES))
DEFAULT_THEME_NAMES: Final[Mapping[str, str]] = MappingProxyType({"auto": "voidcode-dark", "dark": "voidcode-dark", "light": "voidcode-light"})
_THEME_MODES: Final[Mapping[str, str]] = MappingProxyType({**{name: item["mode"] for name, item in _UPSTREAM_THEMES.items()}, **_VOIDCODE_THEMES})

FG_TOKENS: Final[frozenset[str]] = frozenset(get_args(_ForegroundToken.__value__))

#: The ``ThemeBg`` members (``theme/schema.ts:72-79``) this TUI renders.
BG_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "userMessageBg",
        "toolPendingBg",
        "toolSuccessBg",
        "toolErrorBg",
        "statusLineBg",
    }
)


def _voidcode_palette(
    *,
    accent: str,
    text: str,
    muted: str,
    dim: str,
    border: str,
    success: str,
    error: str,
    warning: str,
    code: str,
    string: str,
    user_surface: str,
    pending_surface: str,
    success_surface: str,
    error_surface: str,
    status_surface: str,
) -> Mapping[str, str | int]:
    fg = {token: text for token in FG_TOKENS}
    fg.update(
        {
            "accent": accent,
            "border": border,
            "borderAccent": accent,
            "success": success,
            "error": error,
            "warning": warning,
            "muted": muted,
            "dim": dim,
            "text": "",
            "thinkingText": muted,
            "userMessageText": text,
            "toolTitle": text,
            "mdHeading": accent,
            "mdLink": accent,
            "mdLinkUrl": muted,
            "mdCode": code,
            "mdCodeBlock": muted,
            "mdQuote": muted,
            "mdHr": border,
            "mdListBullet": accent,
            "toolDiffAdded": success,
            "toolDiffRemoved": error,
            "toolDiffContext": muted,
            "syntaxComment": dim,
            "syntaxKeyword": accent,
            "syntaxFunction": success,
            "syntaxVariable": text,
            "syntaxString": string,
            "syntaxNumber": warning,
            "syntaxOperator": accent,
            "syntaxPunctuation": muted,
            "thinkingHigh": accent,
            "statusLineSep": border,
            "statusLineModel": accent,
            "statusLinePath": text,
            "statusLineContext": muted,
            "statusLineCost": warning,
        }
    )
    bg = {
        "userMessageBg": user_surface,
        "toolPendingBg": pending_surface,
        "toolSuccessBg": success_surface,
        "toolErrorBg": error_surface,
        "statusLineBg": status_surface,
    }
    return MappingProxyType(fg | bg)


_VOIDCODE_DARK = _voidcode_palette(
    accent="#58c7f3",
    text="#e8edf2",
    muted="#aab7c4",
    dim="#748391",
    border="#344250",
    success="#70d6a1",
    error="#ff6b78",
    warning="#f2bc68",
    code="#9be0c1",
    string="#e5c07b",
    user_surface="#18232d",
    pending_surface="#202a34",
    success_surface="#17271f",
    error_surface="#321e24",
    status_surface="#111920",
)
_VOIDCODE_LIGHT = _voidcode_palette(
    accent="#176f93",
    text="#172b3a",
    muted="#526779",
    dim="#738696",
    border="#bdcbd6",
    success="#267448",
    error="#a42f3d",
    warning="#805500",
    code="#176746",
    string="#8b4c15",
    user_surface="#e6f2f8",
    pending_surface="#eef3f7",
    success_surface="#e8f6ec",
    error_surface="#fbebed",
    status_surface="#eaf1f6",
)
_VOIDCODE_HIGH_CONTRAST = _voidcode_palette(
    accent="#00ffff",
    text="#ffffff",
    muted="#dddddd",
    dim="#bbbbbb",
    border="#ffffff",
    success="#00ff00",
    error="#ff5555",
    warning="#ffff00",
    code="#00ffaf",
    string="#ffd75f",
    user_surface="#000000",
    pending_surface="#111111",
    success_surface="#001a00",
    error_surface="#240000",
    status_surface="#000000",
)
_ORIGINAL_PALETTES: Final[Mapping[str, Mapping[str, str | int]]] = MappingProxyType(
    {
        "voidcode-dark": _VOIDCODE_DARK,
        "voidcode-light": _VOIDCODE_LIGHT,
        "voidcode-high-contrast": _VOIDCODE_HIGH_CONTRAST,
    }
)


def _resolve_palette_value(
    value: str | int,
    variables: Mapping[str, str | int],
    token: str,
    active: frozenset[str] = frozenset(),
) -> str | int:
    if isinstance(value, bool):
        raise ValueError(f"theme color {token!r} has an invalid value")
    if isinstance(value, int):
        if not 0 <= value <= 255:
            raise ValueError(f"theme color {token!r} ANSI index must be between 0 and 255")
        return value
    if not isinstance(value, str):
        raise ValueError(f"theme color {token!r} has an invalid value")
    if value in ("", "terminal-default"):
        if value == "terminal-default" and token not in BG_TOKENS:
            raise ValueError(f"theme color {token!r} cannot use terminal-default")
        return ""
    if value.startswith("#"):
        if len(value) != 7 or any(char not in "0123456789abcdefABCDEF" for char in value[1:]):
            raise ValueError(f"theme color {token!r} must be #RRGGBB")
        return value.lower()
    if value not in variables:
        raise ValueError(f"theme color {token!r} references unknown variable {value!r}")
    if value in active:
        raise ValueError(f"theme color {token!r} has a reference cycle at variable {value!r}")
    return _resolve_palette_value(variables[value], variables, token, active | {value})


@lru_cache(maxsize=100)
def _load_upstream_palette(name: str) -> Mapping[str, str | int]:
    entry = _UPSTREAM_THEMES[name]
    source = _RESOURCE_ROOT.joinpath(*entry["source"].split("/"))
    raw = json.loads(source.read_text(encoding="utf-8"))
    variables = raw.get("vars")
    colors = raw.get("colors")
    if not isinstance(variables, dict) or not isinstance(colors, dict):
        raise ValueError(f"invalid bundled palette source: {source}")
    tokens = FG_TOKENS | BG_TOKENS
    missing = sorted(tokens - colors.keys())
    if missing:
        raise ValueError(f"bundled palette '{name}' is missing renderer color tokens: {', '.join(missing)}")
    return MappingProxyType({token: _resolve_palette_value(colors[token], variables, f"{name}.{token}") for token in tokens})


def _split(values: Mapping[str, str | int]) -> tuple[Mapping[str, str | int], Mapping[str, str | int]]:
    """Partition a palette into ``(foreground, background)`` token maps.

    Mirrors ``theme/loader.ts:169-177``: background keys are recognised by name,
    everything else is a foreground token.
    """
    fg = {key: value for key, value in values.items() if key in FG_TOKENS}
    bg = {key: value for key, value in values.items() if key in BG_TOKENS}
    return MappingProxyType(fg), MappingProxyType(bg)


# ---------------------------------------------------------------------------
# Glyph tables (theme/symbols.ts presets, restricted to the blocks we render)
# ---------------------------------------------------------------------------

#: ``UNICODE_SYMBOLS`` (``theme/symbols.ts:369-1101``), restricted to the keys
#: this TUI resolves. Keys keep omp's dotted ``SymbolKey`` spelling;
#: ``nav.cursor`` stays for list/select rows.
_UNICODE_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "status.success": "✔",
        "status.error": "✘",
        "status.disabled": "⦸",
        "nav.cursor": "❯",
        "tree.horizontal": "─",
        "diff.gutterVertical": "│",
        "diff.indentArrow": "↳",
        "diff.indentDot": "·",
        "boxRound.topLeft": "╭",
        "boxRound.topRight": "╮",
        "boxRound.bottomLeft": "╰",
        "boxRound.bottomRight": "╯",
        "boxRound.horizontal": "─",
        "boxRound.vertical": "│",
        "sep.dot": " · ",
        "icon.model": "⬢",
        "icon.folder": "[D]",
        "icon.search": "[/]",
        "icon.file": "[F]",
        "icon.context": "ctx:",
        "icon.job": "bg",
        "thinking.minimal": "○ min",
        "thinking.low": "◔ low",
        "thinking.medium": "◑ med",
        "thinking.high": "◒ high",
        "thinking.xhigh": "◕ xhigh",
        "thinking.max": "◉ max",
        "checkbox.checked": "☑",
        "checkbox.unchecked": "☐",
        "radio.selected": "◉",
        "radio.unselected": "○",
        "format.bracketLeft": "⟦",
        "format.bracketRight": "⟧",
        "text.truncation": "…",
        "text.streamEllipsis": "⋯",
        "tool.write": "✎",
        "tool.edit": "✎",
        "tool.bash": "❯",
        "tool.webSearch": "⌕",
        "tool.eval": "▶",
        "tool.job": "job",
        "tool.task": "⇶",
        "tool.todo": "☑",
        "tool.memory": "mem",
        "statusline.status": "●",
        "statusline.mode": "◈",
        "statusline.cost": "$",
        "statusline.session": "◷",
        "statusline.tokenIn": "↑",
        "statusline.tokenOut": "↓",
        "statusline.cacheRead": "↙",
        "statusline.cacheWrite": "↗",
        "statusline.cacheHit": "%",
        "sep.rule": "─",
        "tool.ask": "?",
    }
)
_EMOJI_SYMBOLS: Final[Mapping[str, str]] = _UNICODE_SYMBOLS

#: ``ASCII_SYMBOLS`` (``theme/symbols.ts:1103-1379``), same keys.
_ASCII_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "status.success": "[ok]",
        "status.error": "[!!]",
        "status.disabled": "[ ]",
        "nav.cursor": ">",
        "diff.gutterVertical": "|",
        "diff.indentArrow": "->",
        "diff.indentDot": ".",
        "tree.horizontal": "-",
        "boxRound.topLeft": "+",
        "boxRound.topRight": "+",
        "boxRound.bottomLeft": "+",
        "boxRound.bottomRight": "+",
        "boxRound.horizontal": "-",
        "boxRound.vertical": "|",
        "sep.dot": " - ",
        "icon.model": "[M]",
        "icon.folder": "[D]",
        "icon.search": "[/]",
        "icon.file": "[F]",
        "icon.context": "ctx:",
        "icon.job": "bg",
        "thinking.minimal": "[min]",
        "thinking.low": "[low]",
        "thinking.medium": "[med]",
        "thinking.high": "[high]",
        "thinking.xhigh": "[xhi]",
        "thinking.max": "[max]",
        "checkbox.checked": "[x]",
        "checkbox.unchecked": "[ ]",
        "radio.selected": "(o)",
        "radio.unselected": "( )",
        "format.bracketLeft": "[",
        "text.truncation": "...",
        "text.streamEllipsis": "...",
        "format.bracketRight": "]",
        "tool.write": "+f",
        "tool.edit": "~",
        "tool.bash": "$",
        "tool.webSearch": "web",
        "tool.eval": ">_",
        "tool.job": "job",
        "tool.task": ">>>",
        "tool.todo": "[x]",
        "tool.memory": "mem",
        "statusline.status": "*",
        "statusline.mode": "M",
        "statusline.cost": "$",
        "statusline.session": "S",
        "statusline.tokenIn": "in",
        "statusline.tokenOut": "out",
        "statusline.cacheRead": "cr",
        "statusline.cacheWrite": "cw",
        "statusline.cacheHit": "%",
        "sep.rule": "-",
        "tool.ask": "?",
    }
)

_NERD_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        **_UNICODE_SYMBOLS,
        "status.success": "\uf00c",
        "status.error": "\uf00d",
        "icon.folder": "\uf07b",
        "icon.file": "\uf15b",
        "icon.search": "\uf002",
        "icon.model": "\uf121",
    }
)
SYMBOL_PRESETS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {"unicode": _UNICODE_SYMBOLS, "ascii": _ASCII_SYMBOLS, "nerd": _NERD_SYMBOLS}
)
DEFAULT_GLYPH_PRESET: Final[str] = "ascii"

#: ``SPINNER_FRAMES`` (``theme/symbols.ts:1385-1401``), ported verbatim;
#: only the ``status`` animation is ever advanced.
SPINNER_FRAMES: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = MappingProxyType(
    {
        "unicode": MappingProxyType({"status": ("⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷")}),
        "ascii": MappingProxyType({"status": ("|", "/", "-", "\\")}),
    }
)

#: Glyph kind -> omp ``SymbolKey`` for the tool identity icon
#: (``theme/symbols.ts:264-288``; omp hard-codes the key at each tool's call
#: site, this is the single shared table).
TOOL_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "apply_workspace_edit": "tool.edit",
        "ast_grep": "icon.search",
        "background_process": "tool.job",
        "edit": "tool.edit",
        "glob": "icon.search",
        "grep": "icon.search",
        "invoke_tool": "tool.eval",
        "lsp": "icon.search",
        "question": "tool.ask",
        "read": "icon.file",
        "shell_exec": "tool.bash",
        "skill": "tool.memory",
        "task": "tool.task",
        "task_batch": "tool.task",
        "todo": "tool.todo",
        "web_search": "tool.webSearch",
        "write": "tool.write",
        "yield": "tool.eval",
        "default": "tool.eval",
    }
)

#: Prefix for MCP-proxied tools (``mcp/<server>/<tool>``); every server shares one
#: external-capability glyph rather than a per-server guess.
_MCP_TOOL_PREFIX = "mcp/"


# ---------------------------------------------------------------------------
# Glyph accessor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Glyphs:
    """Symbol lookup restricted to a preset (omp ``Theme`` glyph getters).

    ``symbol()`` keeps omp's dotted ``SymbolKey`` names so the ported table can
    be diffed against ``theme/symbols.ts`` directly.
    """

    preset: str
    symbols: Mapping[str, str]
    spinner_frames: Mapping[str, tuple[str, ...]]

    def symbol(self, key: str) -> str:
        """Resolve a ``SymbolKey``; unknown keys render as nothing."""
        return self.symbols.get(key, "")

    def spinner(self, kind: str, frame: int) -> str:
        """Frame ``frame`` of the ``kind`` spinner animation."""
        frames = self.spinner_frames.get(kind) or self.spinner_frames["status"]
        return frames[frame % len(frames)]

    def thinking(self, level: str) -> str:
        """Thinking-level display ("◑ med", "[med]", ...) for ``level``."""
        return self.symbol(f"thinking.{level}")


def _glyphs(preset: str) -> Glyphs:
    resolved = preset if preset in SYMBOL_PRESETS else "unicode"
    frames = SPINNER_FRAMES.get(resolved, SPINNER_FRAMES["unicode"])
    return Glyphs(preset=resolved, symbols=SYMBOL_PRESETS[resolved], spinner_frames=frames)


@lru_cache(maxsize=4)
def _cached_glyphs(preset: str) -> Glyphs:
    return _glyphs(preset)


# ---------------------------------------------------------------------------
# Colour resolution
# ---------------------------------------------------------------------------


# SGR reset patterns used by the resolved/fill style helpers. Ported from
# ``theme-class.ts`` FOREGROUND_RESET_PATTERN / BACKGROUND_RESET_PATTERN: a
# nested reset inside a styled row must re-open the enclosing colour.
_FOREGROUND_RESET = _compile(r"\x1b\[(?:0|39)m")
_BACKGROUND_RESET = _compile(r"\x1b\[(?:0|49)m")

#: The literals rich's ``Console`` accepts for ``color_system``.
ColorSystemName = Literal["standard", "256", "truecolor", "windows"]

#: rich's ``Console`` keys colour depth by name (``rich/console.py:525-530``)
#: while ``Style``/``Color`` take the enum, so both spellings are needed.
COLOR_SYSTEM_NAMES: Final[Mapping[ColorSystem, ColorSystemName]] = MappingProxyType(
    {
        ColorSystem.STANDARD: "standard",
        ColorSystem.EIGHT_BIT: "256",
        ColorSystem.TRUECOLOR: "truecolor",
        ColorSystem.WINDOWS: "windows",
    }
)


def color_system_name(system: ColorSystem | None) -> ColorSystemName | None:
    """Name rich's ``Console`` wants for ``system`` (``None`` = no colour)."""
    return None if system is None else COLOR_SYSTEM_NAMES.get(system, "truecolor")


def _parse_color(value: str | int) -> Color | None:
    """Turn a palette value into a rich colour; ``None`` means "default"."""
    if isinstance(value, int):
        return Color.from_ansi(value)
    if value == "":
        return None
    return Color.parse(value)


def _color_hex(color: Color) -> str:
    """Hex string for a colour -- omp ``resolveToHex`` (``theme/color.ts:79-82``)."""
    if color.type == ColorType.DEFAULT:
        return "#e5e5e7"
    r, g, b = color.get_truecolor()
    return f"#{r:02x}{g:02x}{b:02x}"


def _sgr(color: Color | None, *, background: bool, color_system: ColorSystem | None) -> str:
    """SGR prefix for ``color`` under ``color_system``.

    ``None`` colour means the terminal default: omp emits the foreground-only
    ``\\x1b[39m`` / background-only ``\\x1b[49m`` reset (``theme-class.ts:292-309``).
    A ``None`` colour *system* means "no colour at all" and emits nothing.
    """
    if color_system is None:
        return ""
    if color is None:
        return "\x1b[49m" if background else "\x1b[39m"
    codes = color.downgrade(color_system).get_ansi_codes(foreground=not background)
    return f"\x1b[{';'.join(codes)}m" if codes else ""


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


class Theme:
    """A resolved palette plus its glyph preset.

    Style access is the only way styles may enter the TUI: ``fg``/``bg`` mirror
    omp's foreground-only / background-only reset semantics, and ``style``
    returns a rich :class:`~rich.style.Style` for renderables that need one.
    """

    __slots__ = ("_backgrounds", "_colors", "_glyphs", "_symbol_overrides", "color_system", "mode", "name")

    def __init__(
        self,
        name: str,
        mode: str,
        colors: Mapping[str, str | int],
        backgrounds: Mapping[str, str | int],
        *,
        color_system: ColorSystem | None = ColorSystem.TRUECOLOR,
        glyph_preset: str = DEFAULT_GLYPH_PRESET,
        symbol_overrides: Mapping[str, str] = MappingProxyType({}),
    ) -> None:
        self.name = name
        self.mode = mode
        self._colors = colors
        self._backgrounds = backgrounds
        self.color_system = color_system
        self._glyphs = _cached_glyphs(glyph_preset)
        self._symbol_overrides = symbol_overrides

    # -- colours -----------------------------------------------------------

    def color(self, token: str) -> str | int:
        """Raw palette value for a foreground token."""
        try:
            return self._colors[token]
        except KeyError:
            raise KeyError(f"Unknown theme color: {token}") from None

    def bg_color(self, token: str) -> str | int:
        """Raw palette value for a background token."""
        try:
            return self._backgrounds[token]
        except KeyError:
            raise KeyError(f"Unknown theme background: {token}") from None

    def hex(self, token: str) -> str:
        """Resolved hex for a foreground token (omp ``getColorHex``)."""
        color = _parse_color(self.color(token))
        if color is None:
            return "#000000" if self.mode == "light" else "#e5e5e7"
        return _color_hex(color)

    def fg(self, token: str, text: str) -> str:
        """Wrap ``text`` in the token's foreground colour (fg-only reset)."""
        if self.color_system is None:
            return text
        color = _parse_color(self.color(token))
        return f"{_sgr(color, background=False, color_system=self.color_system)}{text}\x1b[39m"

    def bg(self, token: str, text: str) -> str:
        """Wrap ``text`` in the token's background colour (bg-only reset)."""
        if self.color_system is None:
            return text
        color = _parse_color(self.bg_color(token))
        return f"{_sgr(color, background=True, color_system=self.color_system)}{text}\x1b[49m"

    def fg_resolved(self, token: str, text: str) -> str:
        """Like :meth:`fg`, but substitutes a concrete colour for a default token.

        Ports ``Theme.fgResolved`` (``theme-class.ts:299-303``): a token that
        resolves to the terminal default is replaced by its hex value, and the
        colour is re-applied after any nested SGR reset so it survives styled
        content (markdown rows, syntax highlighting).
        """
        if self.color_system is None:
            return text
        prefix = _sgr(_parse_color(self.color(token)), background=False, color_system=self.color_system)
        if prefix not in ("", "\x1b[39m"):
            return f"{prefix}{text}\x1b[39m"
        resolved = _sgr(_parse_color(self.hex(token)), background=False, color_system=self.color_system)
        return f"{resolved}{_FOREGROUND_RESET.sub(lambda match: match.group(0) + resolved, text)}\x1b[39m"

    def bg_fill(self, token: str, text: str) -> str:
        """Like :meth:`bg`, but re-applies the fill after nested resets.

        Ports ``Theme.bgFill`` (``theme-class.ts:312-321``): block backgrounds
        stay stable even when the row contains ``\\x1b[0m`` from syntax or
        markdown styling.
        """
        if self.color_system is None:
            return text
        fill = self.get_bg_ansi(token)
        if not fill:
            return text
        return f"{fill}{_BACKGROUND_RESET.sub(lambda match: match.group(0) + fill, text)}\x1b[49m"

    def get_fg_ansi(self, token: str) -> str:
        """Bare foreground SGR prefix (omp ``getFgAnsi``)."""
        return _sgr(_parse_color(self.color(token)), background=False, color_system=self.color_system)

    def get_bg_ansi(self, token: str) -> str:
        """Bare background SGR prefix (omp ``getBgAnsi``)."""
        return _sgr(_parse_color(self.bg_color(token)), background=True, color_system=self.color_system)

    def style(
        self,
        token: str,
        *,
        bold: bool = False,
        dim: bool = False,
        italic: bool = False,
        underline: bool = False,
        strike: bool = False,
        reverse: bool = False,
    ) -> Style:
        """Rich style for a foreground token plus the attributes we use."""
        return Style(
            color=_parse_color(self.color(token)),
            bold=bold,
            dim=dim,
            italic=italic,
            underline=underline,
            strike=strike,
            reverse=reverse,
        )

    # -- glyphs ------------------------------------------------------------

    @property
    def glyphs(self) -> Glyphs:
        """Glyph preset resolved for this theme (unicode or ascii)."""
        return self._glyphs

    def symbol(self, key: str, *, preset: str | None = None) -> str:
        """Resolve a semantic glyph, using explicit presets over user overrides."""
        if preset is not None:
            return SYMBOL_PRESETS[preset].get(key, "")
        override = self._symbol_overrides.get(key)
        if override is not None:
            return override
        return SYMBOL_PRESETS[self._glyphs.preset].get(key, "")

    def styled_symbol(self, key: str, token: str) -> str:
        """Resolve a glyph and colour it (omp ``styledSymbol``)."""
        return self.fg(token, self.symbol(key))

    def spinner_frames(self, kind: str = "status") -> tuple[str, ...]:
        """Spinner frames for ``kind`` (omp ``getSpinnerFrames``)."""
        frames = self._glyphs.spinner_frames
        return frames.get(kind) or frames["status"]

    def tool_icon(self, name: str) -> str:
        """Tool identity glyph: ``name`` -> ``tool.*`` symbol (ASCII-safe).

        Unknown names (notably local custom tools) fall back to ``tool.eval``.
        MCP-proxied tools -- ``mcp/<server>/<tool>`` -- share ``icon.search``.
        """
        if name.startswith(_MCP_TOOL_PREFIX):
            return self.symbol("icon.search")
        return self.symbol(TOOL_SYMBOLS.get(name, TOOL_SYMBOLS["default"]))

    # -- rich integration --------------------------------------------------

    def rich_theme(self) -> RichTheme:
        """Rich style table (markdown + ``markdown.*`` names) built from tokens.

        ``rich.markdown`` has no theme object of its own; it resolves element
        styles through the console's theme (``rich/default_styles.py:142-168``).
        Mapping them here keeps every markdown colour token-driven.
        """
        return RichTheme(
            {
                "markdown.paragraph": self.style("text"),
                "markdown.text": self.style("text"),
                "markdown.em": self.style("text", italic=True),
                "markdown.emph": self.style("text", italic=True),
                "markdown.strong": self.style("text", bold=True),
                "markdown.code": self.style("mdCode", bold=True),
                "markdown.code_block": self.style("mdCodeBlock"),
                "markdown.block_quote": self.style("mdQuote"),
                "markdown.list": self.style("mdListBullet"),
                "markdown.item": self.style("text"),
                "markdown.item.bullet": self.style("mdListBullet", bold=True),
                "markdown.item.number": self.style("mdListBullet"),
                "markdown.hr": self.style("mdHr"),
                "markdown.h1": self.style("mdHeading", bold=True),
                "markdown.h1.border": self.style("mdHr"),
                "markdown.h2": self.style("mdHeading", underline=True),
                "markdown.h3": self.style("mdHeading", bold=True),
                "markdown.h4": self.style("mdHeading", italic=True),
                "markdown.h5": self.style("mdLinkUrl", italic=True),
                "markdown.h6": self.style("mdLinkUrl", dim=True),
                "markdown.h7": self.style("mdLinkUrl", italic=True, dim=True),
                "markdown.link": self.style("mdLink"),
                "markdown.link_url": self.style("mdLinkUrl", underline=True),
                "markdown.s": self.style("text", strike=True),
                "markdown.table.border": self.style("border"),
                "markdown.table.header": self.style("mdHeading", bold=True),
                "markdown.kbd": self.style("text", bold=True),
            },
            inherit=True,
        )

    def syntax_theme(self) -> _TokenSyntaxTheme:
        """Pygments syntax styles built from the ``syntax*`` tokens.

        omp highlights from its own token table (``syntaxComment`` …
        ``syntaxPunctuation``); this maps those same tokens onto pygments token
        types so ``rich.syntax.Syntax`` renders the palette we ported instead of
        a hard-coded theme name.
        """
        return _TokenSyntaxTheme(
            default=self.style("text"),
            comment=self.style("syntaxComment", italic=True),
            keyword=self.style("syntaxKeyword"),
            function=self.style("syntaxFunction"),
            variable=self.style("syntaxVariable"),
            string=self.style("syntaxString"),
            number=self.style("syntaxNumber"),
            operator=self.style("syntaxOperator"),
            punctuation=self.style("syntaxPunctuation"),
            error=self.style("error"),
        )


class _TokenSyntaxTheme(SyntaxTheme):
    """``rich.syntax.SyntaxTheme`` implementation backed by theme tokens."""

    __slots__ = (
        "_comment",
        "_default",
        "_error",
        "_function",
        "_keyword",
        "_number",
        "_operator",
        "_punctuation",
        "_string",
        "_variable",
    )

    def __init__(
        self,
        *,
        default: Style,
        comment: Style,
        keyword: Style,
        function: Style,
        variable: Style,
        string: Style,
        number: Style,
        operator: Style,
        punctuation: Style,
        error: Style,
    ) -> None:
        self._default = default
        self._comment = comment
        self._keyword = keyword
        self._function = function
        self._variable = variable
        self._string = string
        self._number = number
        self._operator = operator
        self._punctuation = punctuation
        self._error = error

    def get_style_for_token(self, token_type: Token.Type) -> Style:
        """Map a pygments token type onto the closest themed style."""
        if token_type in Comment:
            return self._comment
        if token_type in Keyword:
            return self._keyword
        if token_type in Name.Function or token_type in Name.Class:
            return self._function
        if token_type in Name:
            return self._variable
        if token_type in String:
            return self._string
        if token_type in Number:
            return self._number
        if token_type in Operator:
            return self._operator
        if token_type in Punctuation:
            return self._punctuation
        if token_type in Generic or token_type in Error:
            return self._error
        return self._default

    def get_background_style(self) -> Style:
        """No background: rows carry their own block background.

        Required by ``rich.syntax.SyntaxTheme`` (abstract method), not by any
        in-repo caller.
        """
        return Style()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _palette_for_name(name: str) -> tuple[str, Mapping[str, str | int]]:
    if not name or _contains_terminal_controls(name):
        raise ValueError(f"theme palette {name!r} must be non-empty and contain no terminal control characters")
    if name in _ORIGINAL_PALETTES:
        return _THEME_MODES[name], _ORIGINAL_PALETTES[name]
    if name in _UPSTREAM_THEMES:
        return _THEME_MODES[name], _load_upstream_palette(name)
    raise ValueError(f"unknown theme palette {name!r}")


def _validate_symbols(preset: str, overrides: Mapping[str, str]) -> None:
    """Reject an unknown glyph preset or a bogus override before rendering."""
    if preset not in SYMBOL_PRESETS:
        raise ValueError(f"symbol preset must be one of: {', '.join(SYMBOL_PRESETS)}")
    known = set().union(*(symbols.keys() for symbols in SYMBOL_PRESETS.values()))
    for key, glyph in overrides.items():
        if not isinstance(key, str) or _contains_terminal_controls(key) or key not in known:
            raise ValueError(f"unknown symbol override {key!r}")
        if not isinstance(glyph, str) or _contains_terminal_controls(glyph):
            raise ValueError(f"symbol override {key!r} must be a string without terminal control characters")


def palette_names(mode: Literal["dark", "light"]) -> tuple[str, ...]:
    """Return the bundled palette names that declare ``mode``."""
    if mode not in ("dark", "light"):
        raise ValueError("palette mode must be dark or light")
    return tuple(sorted(name for name, palette_mode in _THEME_MODES.items() if palette_mode == mode))


def resolve_theme(
    name: str | None = None,
    mode: str = "auto",
    *,
    color_system: ColorSystem | None = ColorSystem.TRUECOLOR,
    glyph_preset: str = DEFAULT_GLYPH_PRESET,
    symbol_overrides: Mapping[str, str] | None = None,
) -> Theme:
    """Resolve one exact palette name and its validated semantic glyph set."""
    if mode not in DEFAULT_THEME_NAMES:
        raise ValueError(f"theme mode must be one of: {', '.join(DEFAULT_THEME_NAMES)}")
    resolved = name if name is not None else DEFAULT_THEME_NAMES["dark" if mode == "auto" else mode]
    palette_mode, palette = _palette_for_name(resolved)
    if mode in ("dark", "light") and palette_mode != mode:
        raise ValueError(f"theme palette '{resolved}' must declare {mode} mode")
    overrides = symbol_overrides or {}
    _validate_symbols(glyph_preset, overrides)
    fg, bg = _split(palette)
    return Theme(
        name=resolved,
        mode=palette_mode,
        colors=fg,
        backgrounds=bg,
        color_system=color_system,
        glyph_preset=glyph_preset,
        symbol_overrides=MappingProxyType(dict(overrides)),
    )
