from __future__ import annotations

import pytest
from rich.color import ColorSystem

from voidcode.runtime.tool_provider import BUILTIN_TOOL_NAMES
from voidcode.tui.theme import (
    BG_TOKENS,
    DEFAULT_THEME_NAMES,
    FG_TOKENS,
    SPINNER_FRAMES,
    SYMBOL_PRESETS,
    THEME_NAMES,
    color_system_name,
    resolve_theme,
)

#: Symbols the transcript and status line resolve; a preset missing one of
#: these renders an invisible segment, so it is part of the theme contract.
REQUIRED_SYMBOLS = (
    "status.success",
    "status.error",
    "status.disabled",
    "tree.horizontal",
    "boxRound.topLeft",
    "boxRound.topRight",
    "boxRound.bottomLeft",
    "boxRound.bottomRight",
    "boxRound.horizontal",
    "boxRound.vertical",
    "sep.dot",
    "sep.powerlineThinLeft",
    "sep.powerlineThinRight",
    "sep.powerlineLeft",
    "sep.powerlineRight",
    "format.bracketLeft",
    "format.bracketRight",
    "icon.model",
    "icon.context",
    "icon.folder",
    "icon.job",
    "tool.bash",
    "tool.write",
    "tool.edit",
)


@pytest.mark.parametrize(
    ("name", "mode", "expected"),
    [
        (None, "auto", "voidcode-dark"),
        (None, "dark", "voidcode-dark"),
        (None, "light", "voidcode-light"),
        ("voidcode-dark", "auto", "voidcode-dark"),
        ("voidcode-light", "auto", "voidcode-light"),
        ("voidcode-dark", "dark", "voidcode-dark"),
        ("voidcode-light", "light", "voidcode-light"),
        # Unknown or mismatched names fall back to the mode's default.
        ("monokai", "dark", "voidcode-dark"),
        ("monokai", "light", "voidcode-light"),
        ("monokai", "auto", "voidcode-dark"),
        ("", "light", "voidcode-light"),
        ("voidcode-dark", "light", "voidcode-light"),
        ("voidcode-light", "dark", "voidcode-dark"),
        (None, "garbage-mode", "voidcode-dark"),
        ("voidcode-light", "garbage-mode", "voidcode-light"),
    ],
)
def test_resolution_matrix(name: str | None, mode: str, expected: str) -> None:
    theme = resolve_theme(name, mode)
    assert theme.name == expected
    assert theme.mode == ("light" if expected == "voidcode-light" else "dark")


def test_registry_is_frozen_to_two_themes() -> None:
    assert THEME_NAMES == frozenset({"voidcode-dark", "voidcode-light"})
    assert dict(DEFAULT_THEME_NAMES) == {
        "auto": "voidcode-dark",
        "dark": "voidcode-dark",
        "light": "voidcode-light",
    }


#: omp leaves these foreground tokens to the terminal's default colour (an empty
#: string in both palettes: ``theme/color.ts:26-29`` emits ``\\x1b[39m``).
TERMINAL_DEFAULT_TOKENS = frozenset({"text", "toolTitle", "userMessageText"})


@pytest.mark.parametrize("name", sorted(THEME_NAMES))
def test_every_token_has_a_value(name: str) -> None:
    theme = resolve_theme(name, "dark" if name == "voidcode-dark" else "light")
    for token in sorted(FG_TOKENS):
        theme.color(token)  # raises KeyError for an undefined token
        assert theme.hex(token).startswith("#"), token
    for token in sorted(BG_TOKENS):
        assert theme.bg_color(token), token
    assert {token for token in FG_TOKENS if theme.color(token) == ""} == TERMINAL_DEFAULT_TOKENS


def test_default_foreground_token_uses_the_fg_only_reset() -> None:
    theme = resolve_theme(None, "dark")
    assert theme.fg("text", "x") == "\x1b[39mx\x1b[39m"
    assert theme.bg("userMessageBg", "x") == "\x1b[48;2;15;18;22mx\x1b[49m"


def test_256_index_tokens_render_as_ansi_256() -> None:
    theme = resolve_theme("voidcode-light", "light")
    assert theme.get_fg_ansi("statusLineCost") == "\x1b[38;5;133m"


def test_color_depth_degrades() -> None:
    truecolor = resolve_theme(None, "dark")
    eight_bit = resolve_theme(None, "dark", color_system=ColorSystem.EIGHT_BIT)
    standard = resolve_theme(None, "dark", color_system=ColorSystem.STANDARD)
    none = resolve_theme(None, "dark", color_system=None)
    assert truecolor.fg("accent", "x") == "\x1b[38;2;0;180;255mx\x1b[39m"
    assert eight_bit.fg("accent", "x") == "\x1b[38;5;39mx\x1b[39m"
    assert standard.fg("accent", "x") == "\x1b[36mx\x1b[39m"
    assert none.fg("accent", "x") == "x"
    assert none.bg("userMessageBg", "x") == "x"
    assert none.style("accent").render("x", color_system=None) == "x"


def test_color_system_names_match_rich_console_keys() -> None:
    assert color_system_name(ColorSystem.TRUECOLOR) == "truecolor"
    assert color_system_name(ColorSystem.EIGHT_BIT) == "256"
    assert color_system_name(None) is None


def test_unknown_token_raises() -> None:
    theme = resolve_theme(None, "dark")
    with pytest.raises(KeyError, match="Unknown theme color"):
        theme.color("not-a-token")
    with pytest.raises(KeyError, match="Unknown theme background"):
        theme.bg_color("not-a-token")


@pytest.mark.parametrize("preset", sorted(SYMBOL_PRESETS))
def test_glyph_presets_cover_every_symbol_the_ui_resolves(preset: str) -> None:
    theme = resolve_theme(None, "dark", glyph_preset=preset)
    for key in REQUIRED_SYMBOLS:
        assert theme.symbol(key), (preset, key)
    assert theme.symbol("format.bracketLeft") in ("⟦", "[")
    assert len(theme.spinner_frames("status")) == len(SPINNER_FRAMES[preset]["status"])


def test_glyphs_fall_back_to_unicode_for_an_unknown_preset() -> None:
    theme = resolve_theme(None, "dark", glyph_preset="not-a-preset")
    assert theme.glyphs.preset == "unicode"
    assert theme.symbol("boxRound.topLeft") == "╭"
    assert resolve_theme(None, "dark", glyph_preset="ascii").symbol("boxRound.topLeft") == "+"


#: Every builtin tool name -> the glyph key its header must draw. Pinned against
#: ``BUILTIN_TOOL_NAMES`` so a rename in ``tools/`` fails here instead of
#: silently degrading to the ``tool.eval`` fallback. MCP tools are covered by
#: the ``mcp/`` prefix rule, not this table.
BUILTIN_TOOL_GLYPHS = {
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
}


def test_builtin_tool_icons_are_pinned_to_the_runtime_names() -> None:
    theme = resolve_theme(None, "dark")
    non_mcp = {name for name in BUILTIN_TOOL_NAMES if not name.startswith("mcp/")}
    assert non_mcp == set(BUILTIN_TOOL_GLYPHS)
    assert {name: theme.tool_icon(name) for name in BUILTIN_TOOL_GLYPHS} == {name: theme.symbol(key) for name, key in BUILTIN_TOOL_GLYPHS.items()}


def test_tool_icons_resolve_for_known_and_unknown_tools() -> None:
    unicode_theme = resolve_theme(None, "dark")
    ascii_theme = resolve_theme(None, "dark", glyph_preset="ascii")
    assert unicode_theme.tool_icon("shell_exec") == "❯"
    assert ascii_theme.tool_icon("shell_exec") == "$"
    assert unicode_theme.tool_icon("mcp/context7/query-docs") == unicode_theme.symbol("icon.search")
    assert unicode_theme.tool_icon("not-a-tool") == unicode_theme.symbol("tool.eval")
    assert unicode_theme.tool_icon("my_custom/tool") == unicode_theme.symbol("tool.eval")


def test_fg_resolved_reapplies_the_colour_after_nested_resets() -> None:
    theme = resolve_theme(None, "dark")
    styled = "\x1b[1mbold\x1b[0m tail"
    painted = theme.fg_resolved("userMessageText", styled)
    assert painted.count("\x1b[38;2;229;229;231m") == 2
    assert painted.endswith("\x1b[39m")


def test_bg_fill_reapplies_the_background_after_nested_resets() -> None:
    theme = resolve_theme(None, "dark")
    filled = theme.bg_fill("userMessageBg", "a\x1b[0mb")
    assert filled.count("\x1b[48;2;15;18;22m") == 2
    assert filled.endswith("\x1b[49m")


def test_syntax_theme_maps_pygments_tokens_to_themed_styles() -> None:
    from pygments.token import Comment, Keyword, Name, String

    theme = resolve_theme(None, "dark")
    syntax = theme.syntax_theme()
    assert syntax.get_style_for_token(Comment) == theme.style("syntaxComment", italic=True)
    assert syntax.get_style_for_token(Keyword) == theme.style("syntaxKeyword")
    assert syntax.get_style_for_token(Name.Function) == theme.style("syntaxFunction")
    assert syntax.get_style_for_token(String) == theme.style("syntaxString")


def test_rich_theme_styles_markdown_from_tokens() -> None:
    theme = resolve_theme(None, "dark")
    markdown_theme = theme.rich_theme()
    assert markdown_theme.styles["markdown.code"].color == theme.style("mdCode", bold=True).color
    assert markdown_theme.styles["markdown.link"].color == theme.style("mdLink").color
