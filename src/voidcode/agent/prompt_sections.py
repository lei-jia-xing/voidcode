from __future__ import annotations

_DYNAMIC_BOUNDARY_MARKER = "<!-- voidcode:dynamic-boundary -->"


def dynamic_boundary_marker() -> str:
    return _DYNAMIC_BOUNDARY_MARKER


def user_append_heading_block() -> str:
    return "User-append: user-authored text is authoritative over generated blocks."


__all__ = ["dynamic_boundary_marker", "user_append_heading_block"]
