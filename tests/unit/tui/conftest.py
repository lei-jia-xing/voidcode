"""Helpers shared by the TUI unit tests: SGR stripping and theme resolution."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import overload

from voidcode.tui.theme import Theme, resolve_theme

#: Matches any SGR sequence (``\x1b[...m``) so assertions compare visible text.
SGR = re.compile(r"\x1b\[[0-9;]*m")


def theme(*, preset: str = "unicode") -> Theme:
    """The dark palette at ``preset`` glyphs (default: unicode)."""
    return resolve_theme(None, "dark", glyph_preset=preset)


@overload
def plain(text: str) -> str: ...
@overload
def plain(rows: Sequence[str]) -> list[str]: ...
def plain(value: str | Sequence[str]) -> str | list[str]:
    """Strip SGR escapes: a string in gives a string out, rows give stripped rows."""
    if isinstance(value, str):
        return SGR.sub("", value)
    return [SGR.sub("", row).rstrip() for row in value]
