"""Fixed semantic rendering: native ANSI styles plus retained omp glyphs.

No palettes, no names, no switching. Built-in slots use the terminal's own
colors (default fg/bg + bright/dim SGR + the 16 ANSI names), so they stay
legible on any dark/light background without probing it. Plugin style data
(``ComponentStyle``) tints only its owning component; see ``tui/views.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pygments.token import Comment, Error, Generic, Keyword, Name, Number, Operator, Punctuation, String
from rich.color import Color, ColorSystem
from rich.style import Style
from rich.syntax import SyntaxTheme, TokenType
from rich.theme import Theme as RichTheme

from voidcode.sdk.icons import Icon
from voidcode.sdk.views import ComponentStyle

__all__ = [
    "SPINNER_FRAMES",
    "SYMBOLS",
    "TOOL_ICON",
    "Style",
    "Styles",
    "color_system_name",
    "markdown_theme",
    "render_icon",
    "resolve_component",
    "style_for",
    "symbol",
    "syntax_theme",
]

#: The literals rich's ``Console`` accepts for ``color_system``.
ColorSystemName = Literal["standard", "256", "truecolor", "windows"]

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


#: Glyphs actually rendered (ported omp ``theme/symbols.ts``;
#: attribution in ``LICENSE.oh-my-pi``). Dead presets/overrides removed.
SYMBOLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "nav.cursor": "❯",
        "boxRound.topLeft": "╭",
        "boxRound.topRight": "╮",
        "boxRound.bottomLeft": "╰",
        "boxRound.bottomRight": "╯",
        "boxRound.horizontal": "─",
        "boxRound.vertical": "│",
    }
)
#: Braille spinner; the only animation advanced.
SPINNER_FRAMES: Final[tuple[str, ...]] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
#: Single generic tool glyph. Tools declare their own icon (SDK contract owns
#: the mark).
TOOL_ICON: Final[str] = "⬡"


def symbol(key: str) -> str:
    """Resolve a semantic glyph; unknown keys render as nothing."""
    return SYMBOLS.get(key, "")


def render_icon(icon: Icon | None) -> str:
    """One tool icon: the declared glyph, control-free.

    ``None`` icon: the generic placeholder. Empty strings render nothing.
    Never Rich/SGR markup: callers print the result as plain ``Text``.
    """
    if icon is None:
        return TOOL_ICON
    from .term import escape_terminal_controls

    return escape_terminal_controls(icon.glyph)


def _sgr(color: Color | None, *, background: bool, color_system: ColorSystem | None) -> str:
    """SGR prefix for ``color`` under ``color_system`` (``None`` = default)."""
    if color_system is None:
        return ""
    if color is None:
        return "\x1b[49m" if background else "\x1b[39m"
    codes = color.downgrade(color_system).get_ansi_codes(foreground=not background)
    return f"\x1b[{';'.join(codes)}m" if codes else ""


#: Built-in semantic slots. ``None`` = terminal default fg (never probed);
#: ANSI names ride the user's own palette on dark and light backgrounds.
_SLOT_COLORS: Final[Mapping[str, str | None]] = MappingProxyType(
    {
        "accent": "bright_blue",
        "border": None,
        "success": "green",
        "error": "bright_red",
        "warning": "yellow",
        "muted": "bright_black",
        "dim": "bright_black",
        "text": None,
        "link": "bright_blue",
        "code": "bright_green",
        "diff_added": "green",
        "diff_removed": "bright_red",
        "diff_context": "bright_black",
        "syntax_comment": "bright_black",
        "syntax_keyword": "bright_blue",
        "syntax_function": "green",
        "syntax_string": "yellow",
        "syntax_number": "yellow",
        "syntax_operator": "bright_blue",
        "syntax_punctuation": "bright_black",
    }
)

_TONE_SLOTS: Final[Mapping[str, str]] = MappingProxyType({"info": "accent", "success": "success", "warning": "warning", "error": "error"})


def resolve_component(style: ComponentStyle | None, *, level: str | None = None) -> tuple[str | None, str | None, str | None]:
    """(fg, bg, tone-slot) for one component per the frozen precedence.

    Explicit fg/bg win; then a non-default tone; then a notice level;
    then the client's built-in slot default (``None`` here = caller slot).
    Values come from validated ``ComponentStyle`` (SDK ingress rejects anything
    else), so rich parses them directly — no renderer-side revalidation.
    """
    tone = style.tone if style is not None else "default"
    fg = style.foreground if style is not None else None
    bg = style.background if style is not None else None
    if tone == "default" and level in ("info", "warning", "error"):
        tone = level
    slot = _TONE_SLOTS.get(tone) if tone != "default" else None
    return fg, bg, slot


def style_for(
    slot: str,
    *,
    bold: bool = False,
    dim: bool = False,
    italic: bool = False,
    underline: bool = False,
    strike: bool = False,
    reverse: bool = False,
    color: str | None = None,
    background: str | None = None,
) -> Style:
    """Rich style for a built-in slot, optionally overridden by explicit colors."""
    name = color if color is not None else _SLOT_COLORS.get(slot)
    return Style(
        color=None if name is None else Color.parse(name),
        bgcolor=None if background is None else Color.parse(background),
        bold=bold,
        dim=dim,
        italic=italic,
        underline=underline,
        strike=strike,
        reverse=reverse,
    )


def _markdown_theme() -> RichTheme:
    """Rich style table for ``rich.markdown`` element names (fixed slots)."""
    return RichTheme(
        {
            "markdown.paragraph": style_for("text"),
            "markdown.text": style_for("text"),
            "markdown.em": style_for("text", italic=True),
            "markdown.emph": style_for("text", italic=True),
            "markdown.strong": style_for("text", bold=True),
            "markdown.code": style_for("code", bold=True),
            "markdown.code_block": style_for("muted"),
            "markdown.block_quote": style_for("muted"),
            "markdown.list": style_for("accent"),
            "markdown.item": style_for("text"),
            "markdown.item.bullet": style_for("accent", bold=True),
            "markdown.item.number": style_for("accent"),
            "markdown.hr": style_for("border"),
            "markdown.h1": style_for("accent", bold=True),
            "markdown.h1.border": style_for("border"),
            "markdown.h2": style_for("accent", underline=True),
            "markdown.h3": style_for("accent", bold=True),
            "markdown.h4": style_for("accent", italic=True),
            "markdown.h5": style_for("muted", italic=True),
            "markdown.h6": style_for("muted", dim=True),
            "markdown.h7": style_for("muted", italic=True, dim=True),
            "markdown.link": style_for("link"),
            "markdown.link_url": style_for("muted", underline=True),
            "markdown.s": style_for("text", strike=True),
            "markdown.table.border": style_for("border"),
            "markdown.table.header": style_for("accent", bold=True),
            "markdown.kbd": style_for("text", bold=True),
        },
        inherit=True,
    )


_MARKDOWN_THEME = _markdown_theme()


def markdown_theme() -> RichTheme:
    """Shared fixed markdown style table (stateless, safe to reuse)."""
    return _MARKDOWN_THEME


class _FixedSyntaxTheme(SyntaxTheme):
    """``rich.syntax.SyntaxTheme`` backed by the fixed slots."""

    __slots__ = ("_styles",)

    def __init__(self) -> None:
        self._styles = {
            "default": style_for("text"),
            "comment": style_for("syntax_comment", italic=True),
            "keyword": style_for("syntax_keyword"),
            "function": style_for("syntax_function"),
            "variable": style_for("text"),
            "string": style_for("syntax_string"),
            "number": style_for("syntax_number"),
            "operator": style_for("syntax_operator"),
            "punctuation": style_for("syntax_punctuation"),
            "error": style_for("error"),
        }

    def get_style_for_token(self, token_type: TokenType) -> Style:
        """Map a pygments token type onto the closest fixed style."""
        if token_type in Comment:
            return self._styles["comment"]
        if token_type in Keyword:
            return self._styles["keyword"]
        if token_type in Name.Function or token_type in Name.Class:
            return self._styles["function"]
        if token_type in Name:
            return self._styles["variable"]
        if token_type in String:
            return self._styles["string"]
        if token_type in Number:
            return self._styles["number"]
        if token_type in Operator:
            return self._styles["operator"]
        if token_type in Punctuation:
            return self._styles["punctuation"]
        if token_type in Generic or token_type in Error:
            return self._styles["error"]
        return self._styles["default"]

    def get_background_style(self) -> Style:
        """No background: rows carry their own styling."""
        return Style()


_SYNTAX_THEME = _FixedSyntaxTheme()


def syntax_theme() -> SyntaxTheme:
    """Shared fixed syntax theme (stateless, safe to reuse)."""
    return _SYNTAX_THEME


class Styles:
    """Color-depth handle: fixed slots rendered under one ``color_system``.

    ``None`` color system strips every SGR code (NO_COLOR / non-tty path).
    """

    __slots__ = ("color_system",)

    def __init__(self, color_system: ColorSystem | None = ColorSystem.TRUECOLOR) -> None:
        self.color_system = color_system

    def fg(self, slot: str, text: str, *, color: str | None = None) -> str:
        """Wrap ``text`` in a foreground colour (fg-only reset)."""
        if self.color_system is None:
            return text
        name = color if color is not None else _SLOT_COLORS.get(slot)
        parsed = None if name is None else Color.parse(name)
        return f"{_sgr(parsed, background=False, color_system=self.color_system)}{text}\x1b[39m"

    def style(
        self,
        slot: str,
        *,
        color: str | None = None,
        background: str | None = None,
        bold: bool = False,
        dim: bool = False,
        italic: bool = False,
        underline: bool = False,
        strike: bool = False,
        reverse: bool = False,
    ) -> Style:
        """Rich style for a slot, with optional explicit fg/bg override."""
        return style_for(
            slot, color=color, background=background, bold=bold, dim=dim, italic=italic, underline=underline, strike=strike, reverse=reverse
        )

    def rich_theme(self) -> RichTheme:
        """Fixed markdown style table."""
        return _MARKDOWN_THEME

    def syntax_theme(self) -> SyntaxTheme:
        """Fixed syntax theme."""
        return _SYNTAX_THEME

    def render(
        self,
        slot: str,
        text: str,
        *,
        bold: bool = False,
        dim: bool = False,
        italic: bool = False,
        underline: bool = False,
        strike: bool = False,
        reverse: bool = False,
    ) -> str:
        """One styled row fragment (bold/reverse headings, statusline)."""
        return self.style(slot, bold=bold, dim=dim, italic=italic, underline=underline, strike=strike, reverse=reverse).render(
            text, color_system=self.color_system
        )
