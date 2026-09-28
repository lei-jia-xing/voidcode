"""Theme tokens, palettes, and glyph tables.

Design data ported from oh-my-pi (https://github.com/can1357/oh-my-pi, MIT).
``path:line`` references below are relative to that checkout
(``/tmp/omp-src``), recorded so the provenance of every ported value stays
visible:

* Token vocabulary (``ThemeColor``/``ThemeBg``): ``packages/tui/src/theme/schema.ts``.
* ``voidcode-dark`` colour values: ``packages/tui/src/theme/defaults/titanium.json``
  (omp's default dark theme -- ``packages/coding-agent/src/modes/settings.ts:59``
  ``theme.dark`` default ``"titanium"``), with ``packages/tui/src/theme/dark.json``
  as the fallback base for tokens titanium omits.
* ``voidcode-light`` colour values: ``packages/tui/src/theme/light.json``
  (``settings.ts`` ``theme.light`` default ``"light"``).
* Glyph tables: ``packages/tui/src/theme/symbols.ts`` -- ``UNICODE_SYMBOLS``
  (``:369``) and ``ASCII_SYMBOLS`` (``:1103``), keyed by omp's own ``SymbolKey``
  names (``:9``).
* Spinner frames: ``packages/tui/src/theme/symbols.ts:1385-1401`` ``SPINNER_FRAMES``.
* ``fg``/``bg`` reset semantics (foreground-only ``\\x1b[39m`` / background-only
  ``\\x1b[49m``): ``packages/tui/src/theme/theme-class.ts:292-309``.

The registry (which names exist, and which name each mode falls back to) is
owned here, not by the runtime config: a name arrives unresolved -- possibly
``None``, possibly a typo -- and :func:`resolve_theme` is the only resolver.

This module is pure: no terminal I/O, no global mutable state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from re import compile as _compile
from types import MappingProxyType
from typing import Final, Literal

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

__all__ = [
    "BG_TOKENS",
    "DEFAULT_THEME_NAMES",
    "FG_TOKENS",
    "SPINNER_FRAMES",
    "THEME_NAMES",
    "SYMBOL_PRESETS",
    "Glyphs",
    "Theme",
    "resolve_theme",
]

#: Every palette the registry knows. Frozen: another worker whitelists these.
THEME_NAMES: Final[frozenset[str]] = frozenset({"voidcode-dark", "voidcode-light"})

#: Mode -> palette name. Mirrors omp's ``getAutoThemeMapping`` defaults
#: (``settings.ts`` ``theme.dark = "titanium"``, ``theme.light = "light"``).
DEFAULT_THEME_NAMES: Final[Mapping[str, str]] = MappingProxyType({"auto": "voidcode-dark", "dark": "voidcode-dark", "light": "voidcode-light"})

_THEME_MODES: Final[Mapping[str, str]] = MappingProxyType({"voidcode-dark": "dark", "voidcode-light": "light"})

#: The ``ThemeColor`` members (``theme/schema.ts:9-70``) this TUI renders.
FG_TOKENS: Final[frozenset[str]] = frozenset(
    {
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
    }
)

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

# Provenance: defaults/titanium.json, vars resolved. A missing token inherits
# from dark.json.
#
# Audited token-by-token against the ported source: every value matches
# titanium.json/dark.json (dark) and light.json (light), and none is invented.
# Carried only the tokens :data:`FG_TOKENS` / :data:`BG_TOKENS` name -- the
# vocabulary this TUI actually resolves; a token omp declares but no renderer
# reads is in neither the token set nor the palettes. Not carried: dark.json's
# undeclared ``link``, the ``thinking*`` colour levels other than the two this
# TUI renders (``thinkingText``, ``thinkingHigh``), and the ``export`` group
# ``pageBg``/``cardBg``/``infoBg`` (omp's page/card chrome).
_TITANIUM: Final[Mapping[str, str | int]] = MappingProxyType(
    {
        "accent": "#00b4ff",
        "border": "#2a3038",
        "borderAccent": "#00b4ff",
        "success": "#00ff88",
        "error": "#ff4757",
        "warning": "#ffb347",
        "muted": "#9ca3b0",
        "dim": "#6b7280",
        "text": "",
        "thinkingText": "#9ca3b0",
        "userMessageBg": "#0f1216",
        "userMessageText": "",
        "toolPendingBg": "#0f1216",
        "toolSuccessBg": "#0f1216",
        "toolErrorBg": "#1a0f10",
        "toolTitle": "",
        "mdHeading": "#00b4ff",
        "mdLink": "#00b4ff",
        "mdLinkUrl": "#0082b3",
        "mdCode": "#00ff88",
        "mdCodeBlock": "#9ca3b0",
        "mdQuote": "#9ca3b0",
        "mdHr": "#2a3038",
        "mdListBullet": "#00b4ff",
        "toolDiffAdded": "#00ff88",
        "toolDiffRemoved": "#ff4757",
        "toolDiffContext": "#9ca3b0",
        "syntaxComment": "#6b7280",
        "syntaxKeyword": "#00b4ff",
        "syntaxFunction": "#00ff88",
        "syntaxVariable": "#e8ecf4",
        "syntaxString": "#d4c090",
        "syntaxNumber": "#ffb347",
        "syntaxOperator": "#00b4ff",
        "syntaxPunctuation": "#9ca3b0",
        "thinkingHigh": "#00b4ff",
        "statusLineBg": "#0f1216",
        "statusLineSep": "#2a3038",
        "statusLineModel": "#00b4ff",
        "statusLinePath": "#e8ecf4",
        "statusLineContext": "#9ca3b0",
        "statusLineCost": "#d4c090",
    }
)

# Provenance: theme/light.json, vars resolved. Integer values are 256-palette
# indices exactly as omp writes them (theme/color.ts:31-35 emits ``38;5;<n>``).
_LIGHT: Final[Mapping[str, str | int]] = MappingProxyType(
    {
        "accent": "#5a8080",
        "border": "#547da7",
        "borderAccent": "#5a8080",
        "success": "#588458",
        "error": "#aa5555",
        "warning": "#9a7326",
        "muted": "#6c6c6c",
        "dim": "#767676",
        "text": "",
        "thinkingText": "#6c6c6c",
        "userMessageBg": "#e8e8e8",
        "userMessageText": "",
        "toolPendingBg": "#e8e8f0",
        "toolSuccessBg": "#e8f0e8",
        "toolErrorBg": "#f0e8e8",
        "toolTitle": "",
        "mdHeading": "#9a7326",
        "mdLink": "#547da7",
        "mdLinkUrl": "#767676",
        "mdCode": "#5a8080",
        "mdCodeBlock": "#588458",
        "mdQuote": "#6c6c6c",
        "mdHr": "#6c6c6c",
        "mdListBullet": "#588458",
        "toolDiffAdded": "#588458",
        "toolDiffRemoved": "#aa5555",
        "toolDiffContext": "#6c6c6c",
        "syntaxComment": "#008000",
        "syntaxKeyword": "#0000ff",
        "syntaxFunction": "#795e26",
        "syntaxVariable": "#001080",
        "syntaxString": "#a31515",
        "syntaxNumber": "#098658",
        "syntaxOperator": "#000000",
        "syntaxPunctuation": "#000000",
        "thinkingHigh": "#875f87",
        "statusLineBg": "#e0e0e0",
        "statusLineSep": "#808080",
        "statusLineModel": "#875f87",
        "statusLinePath": "#005f87",
        "statusLineContext": "#5f5f87",
        "statusLineCost": 133,
    }
)

_PALETTES: Final[Mapping[str, Mapping[str, str | int]]] = MappingProxyType({"voidcode-dark": _TITANIUM, "voidcode-light": _LIGHT})


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
#: this TUI resolves. Keys keep omp's dotted ``SymbolKey`` spelling, except
#: ``inputCursor`` -- omp's ``symbols.inputCursor`` is its own flat theme field,
#: not a dotted ``SymbolKey`` (``theme/tui-adapters.ts:156-161``), and it is the
#: composer's end-of-line caret. ``nav.cursor`` stays for list/select rows.
_UNICODE_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "status.success": "✔",
        "status.error": "✘",
        "status.disabled": "⦸",
        "nav.cursor": "❯",
        "inputCursor": "▏",
        "tree.horizontal": "─",
        "boxRound.topLeft": "╭",
        "boxRound.topRight": "╮",
        "boxRound.bottomLeft": "╰",
        "boxRound.bottomRight": "╯",
        "boxRound.horizontal": "─",
        "boxRound.vertical": "│",
        "sep.powerlineLeft": "▶",
        "sep.powerlineRight": "◀",
        "sep.powerlineThinLeft": ">",
        "sep.powerlineThinRight": "<",
        "sep.dot": " · ",
        "icon.model": "⬢",
        "icon.folder": "📁",
        "icon.search": "🔍",
        "icon.file": "📄",
        "icon.context": "◫",
        "icon.job": "⚙",
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
        "tool.write": "✎",
        "tool.edit": "✎",
        "tool.bash": "❯",
        "tool.webSearch": "⌕",
        "tool.eval": "▶",
        "tool.job": "⚙",
        "tool.task": "⇶",
        "tool.todo": "☑",
        "tool.memory": "🧠",
        "tool.ask": "?",
    }
)

#: ``ASCII_SYMBOLS`` (``theme/symbols.ts:1103-1379``), same keys.
_ASCII_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "status.success": "[ok]",
        "status.error": "[!!]",
        "status.disabled": "[ ]",
        "nav.cursor": ">",
        "inputCursor": "|",
        "tree.horizontal": "-",
        "boxRound.topLeft": "+",
        "boxRound.topRight": "+",
        "boxRound.bottomLeft": "+",
        "boxRound.bottomRight": "+",
        "boxRound.horizontal": "-",
        "boxRound.vertical": "|",
        "sep.powerlineLeft": ">",
        "sep.powerlineRight": "<",
        "sep.powerlineThinLeft": ">",
        "sep.powerlineThinRight": "<",
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
        "tool.ask": "?",
    }
)

SYMBOL_PRESETS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType({"unicode": _UNICODE_SYMBOLS, "ascii": _ASCII_SYMBOLS})

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
#: site, this is the single table our transcript blocks share).
TOOL_SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "apply_patch": "tool.edit",
        "apply_workspace_edit": "tool.edit",
        "ast_grep": "icon.search",
        "background_process": "tool.job",
        "edit": "tool.edit",
        "glob": "icon.search",
        "grep": "icon.search",
        "invoke_tool": "tool.eval",
        "lsp": "icon.search",
        "multi_edit": "tool.edit",
        "question": "tool.ask",
        "read": "icon.file",
        "shell_exec": "tool.bash",
        "skill": "tool.memory",
        "task": "tool.task",
        "task_batch": "tool.task",
        "todo": "tool.todo",
        "web_fetch": "tool.webSearch",
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

    __slots__ = ("_backgrounds", "_colors", "_glyphs", "color_system", "mode", "name")

    def __init__(
        self,
        name: str,
        mode: str,
        colors: Mapping[str, str | int],
        backgrounds: Mapping[str, str | int],
        *,
        color_system: ColorSystem | None = ColorSystem.TRUECOLOR,
        glyph_preset: str = "unicode",
    ) -> None:
        self.name = name
        self.mode = mode
        self._colors = colors
        self._backgrounds = backgrounds
        self.color_system = color_system
        self._glyphs = _cached_glyphs(glyph_preset)

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

    def symbol(self, key: str) -> str:
        """Resolve a ``SymbolKey`` from the active glyph preset."""
        return self._glyphs.symbol(key)

    def input_cursor(self) -> str:
        """The composer's end-of-line caret: ``▏``, or ``|`` on the ascii preset.

        omp ``symbols.inputCursor`` (``theme/tui-adapters.ts:156-161``); the
        editor's caret getter is ``editor.ts:1155-1165``.
        """
        return self._glyphs.symbol("inputCursor")

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


def _build(name: str, glyph_preset: str, color_system: ColorSystem | None) -> Theme:
    fg, bg = _split(_PALETTES[name])
    return Theme(
        name=name,
        mode=_THEME_MODES[name],
        colors=fg,
        backgrounds=bg,
        color_system=color_system,
        glyph_preset=glyph_preset,
    )


@lru_cache(maxsize=32)
def _resolve_cached(name: str, _mode: str, glyph_preset: str, color_system: str | None) -> Theme:
    system = None if color_system is None else ColorSystem[color_system]
    return _build(name, glyph_preset, system)


def resolve_theme(
    name: str | None = None,
    mode: str = "auto",
    *,
    color_system: ColorSystem | None = ColorSystem.TRUECOLOR,
    glyph_preset: str = "unicode",
) -> Theme:
    """Resolve an unresolved palette name + mode into a :class:`Theme`.

    ``name`` may be ``None`` or a typo: the mode's default palette is used
    instead. An unknown ``mode`` degrades to ``auto``. ``glyph_preset`` selects
    omp's unicode or ascii symbol table (pass ``"ascii"`` when the terminal
    cannot render the unicode glyphs).
    """
    fallback_mode = mode if mode in DEFAULT_THEME_NAMES else "auto"
    default_name = DEFAULT_THEME_NAMES[fallback_mode]
    if name in THEME_NAMES and (fallback_mode == "auto" or _THEME_MODES[name] == fallback_mode):
        resolved = name
    else:
        resolved = default_name
    system = None if color_system is None else color_system.name
    return _resolve_cached(resolved, fallback_mode, glyph_preset, system)
