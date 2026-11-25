"""UTF-8 bounds and untrusted-text rendering for the todo plugin."""

from __future__ import annotations

import html
from collections.abc import Sequence

from . import limits

TODO_TEXT_TRUNCATION_SUFFIX = "\n... [todo list truncated]"


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def truncate_utf8(value: str, limit: int, *, suffix: str = "") -> str:
    """Cut UTF-8 text to at most ``limit`` bytes without splitting a code point."""
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    marker = suffix.encode("utf-8")
    if len(marker) >= limit:
        return marker[:limit].decode("utf-8", errors="ignore")
    budget = limit - len(marker)
    return raw[:budget].decode("utf-8", errors="ignore") + suffix


def require_text_bytes(value: str, *, field: str, limit: int) -> str:
    if utf8_len(value) > limit:
        raise ValueError(f"todo {field} exceeds {limit} UTF-8 bytes")
    return value


def clean_prompt_text(value: str, limit: int) -> str:
    """One-line escaped text for untrusted prompt content."""
    return truncate_utf8(html.escape(" ".join(value.split()), quote=False), limit)


def display_text(value: str, limit: int) -> str:
    """One-line bounded text for client labels and receipts."""
    return truncate_utf8(" ".join(value.split()), limit)


def untrusted_todo_block(headline: str, body_lines: Sequence[str], limit: int) -> str:
    body = truncate_utf8("\n".join(body_lines), max(0, limit - 512), suffix=TODO_TEXT_TRUNCATION_SUFFIX)
    return f"{headline}\n<untrusted_todos>\n{body}\n</untrusted_todos>"


def bounded_command_output(text: str) -> str:
    return truncate_utf8(text, limits.MAX_TODO_COMMAND_OUTPUT_BYTES, suffix="\n... [todo output truncated]")
