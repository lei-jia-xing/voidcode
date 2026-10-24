"""Shared truncation bounds used by core, runtime, and plugins."""

from __future__ import annotations

from typing import Final

CHILD_PROMPT_MAX_BYTES: Final[int] = 256 * 1024
DIAGNOSTIC_TEXT_CHARS: Final[int] = 4000
DIAGNOSTIC_DEPTH: Final[int] = 8
DIAGNOSTIC_ITEMS: Final[int] = 256
TOOL_OUTPUT_BYTES: Final[int] = 50 * 1024
