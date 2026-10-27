"""Tool-owned display icons: a glyph plus its explicit ASCII fallback.

A tool declares its own identity; clients never map tool names to glyphs.
Modes are explicit (`glyph | ascii`, default glyph): the renderer prints
`glyph` in glyph mode, `fallback` in ascii mode, and a generic placeholder
when a tool declares no icon. No font detection exists or is promised —
UTF-8 proves bytes travel, not that the font has the codepoint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Icon:
    """One tool's display identity: preferred glyph + printable-ASCII fallback.

    Both fields are required and tool-owned. Empty string is a real value
    (render nothing), distinct from a missing icon (client generic).
    """

    glyph: str
    fallback: str

    def __post_init__(self) -> None:
        if not isinstance(self.glyph, str) or not isinstance(self.fallback, str):
            raise ValueError("icon glyph and fallback must be strings")
        if any(not 0x20 <= ord(char) <= 0x7E for char in self.fallback):
            raise ValueError("icon fallback must be printable ASCII (empty allowed)")
