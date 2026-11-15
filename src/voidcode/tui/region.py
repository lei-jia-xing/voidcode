"""Typed live-frame layout and row diffing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["KittyImagePlacement", "TuiFrame", "diff_rows"]


@dataclass(frozen=True, slots=True)
class KittyImagePlacement:
    sha256: str
    row: int
    column: int
    columns: int
    placement_id: int


@dataclass(frozen=True, slots=True)
class TuiFrame:
    transcript_rows: tuple[str, ...] = ()
    chrome_rows: tuple[str, ...] = ()
    cursor: tuple[int, int] | None = None
    image: KittyImagePlacement | None = None

    @property
    def rows(self) -> tuple[str, ...]:
        return (*self.transcript_rows, *self.chrome_rows)


def diff_rows(previous: Sequence[str], current: Sequence[str]) -> tuple[tuple[int, str], ...]:
    """Rows of ``current`` that differ from ``previous`` (index, text) pairs.

    Trailing rows present in ``previous`` but not in ``current`` are *not*
    reported: the caller erases those by clearing from the first missing row
    down. Rows are compared by exact content -- two identical frames therefore
    produce no changes at all, which is what makes a settled repaint free.
    """
    changes: list[tuple[int, str]] = []
    for index, row in enumerate(current):
        if index >= len(previous) or previous[index] != row:
            changes.append((index, row))
    return tuple(changes)
