"""Shared truncation bounds used by core, runtime, and plugins."""

from __future__ import annotations

from typing import Final

CHILD_PROMPT_MAX_BYTES: Final[int] = 256 * 1024
DIAGNOSTIC_TEXT_CHARS: Final[int] = 4000
DIAGNOSTIC_DEPTH: Final[int] = 8
DIAGNOSTIC_ITEMS: Final[int] = 256
DEBUG_CONTENT_CHARS: Final[int] = 2_000
PROMPT_FRAGMENT_PREVIEW_CHARS: Final[int] = 240
POLICY_DIAGNOSTIC_CHARS: Final[int] = 256
POLICY_DIAGNOSTIC_LIMIT: Final[int] = 32
TOOL_OUTPUT_LINES: Final[int] = 1000
TOOL_OUTPUT_BYTES: Final[int] = 50 * 1024
MODEL_FIELD_CHARS: Final[int] = 4000


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated: kept first {limit} of {len(text)} chars]"
