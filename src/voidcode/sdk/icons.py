"""Tool-owned display icons: one Unicode glyph, rendered as declared.

A tool declares its own identity; clients never map tool names to glyphs.
The renderer prints the glyph; when a tool declares no icon the client
shows its generic placeholder. No font detection exists or is promised —
UTF-8 proves bytes travel, not that the font has the codepoint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Icon:
    """One tool's display identity: its preferred glyph.

    The field is required and tool-owned. Empty string is a real value
    (render nothing), distinct from a missing icon (client generic).
    """

    glyph: str

    def __post_init__(self) -> None:
        if not isinstance(self.glyph, str):
            raise ValueError("icon glyph must be a string")
