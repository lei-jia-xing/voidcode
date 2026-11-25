"""Documented ingestion and prompt bounds shared by the skills modules.

Discovery bounds the number of entries and each file's dimensions; catalog/context and
diagnostic renderers bound their own contributions. Violations are reported per entry
rather than silently dropped.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

#: Discovery and ingestion bounds. Search roots themselves stay configured (absolute
#: roots are trusted as given).
MAX_SKILLS = 64
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_NAME_CHARS = 128
MAX_SKILL_DESCRIPTION_CHARS = 1024
MAX_SKILL_BODY_BYTES = 48 * 1024
MAX_SKILL_CATALOG_BYTES = 8 * 1024
MAX_SKILL_CONTEXT_BYTES = 64 * 1024
MAX_SKILL_DIAGNOSTICS = 32
MAX_SKILL_DIAGNOSTIC_CHARS = 300
MAX_SKILL_DIAGNOSTIC_BYTES = 2 * 1024


def bounded_message(message: object, *, limit: int = MAX_SKILL_DIAGNOSTIC_CHARS) -> str:
    """One single-line diagnostic bounded by characters, visibly marked when cut."""
    compact = " ".join(str(message).split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def truncate_utf8(text: str, *, limit: int, marker: str) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes and append a visible marker."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= limit:
        return marker_bytes[:limit].decode("utf-8", errors="ignore")
    return encoded[: limit - len(marker_bytes)].decode("utf-8", errors="ignore") + marker


def bounded_sorted_files(
    root: Path,
    *,
    limit: int,
    matches: Callable[[str], bool],
) -> tuple[Path, ...]:
    """Collect at most ``limit`` matching files in deterministic walk order.

    ``os.walk`` is sorted per directory and never follows symlinked directories. The
    caller passes ``cap + 1`` to detect that at least one candidate was over the cap
    without materialising the whole tree.
    """
    if limit < 1:
        return ()
    found: list[Path] = []

    def raise_walk_error(exc: OSError) -> None:
        raise exc

    for current, directories, files in os.walk(root, onerror=raise_walk_error, followlinks=False):
        directories.sort()
        for name in sorted(files):
            if not matches(name):
                continue
            found.append(Path(current) / name)
            if len(found) >= limit:
                return tuple(found)
    return tuple(found)
