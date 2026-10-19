"""Typed live-frame layout and append-only settled-row commit boundary.

Only rows returned by the transcript's settle ledger may reach scrollback.
Unsettled transcript rows and chrome remain in the bounded live viewport; a
resize drops only older transcript rows and never promotes them to committed
history.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = ["LiveRegion", "TuiFrame", "diff_rows"]


@dataclass(frozen=True, slots=True)
class TuiFrame:
    transcript_rows: tuple[str, ...] = ()
    chrome_rows: tuple[str, ...] = ()
    cursor: tuple[int, int] | None = None

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


class LiveRegion:
    """Bounded live viewport; only explicit settled batches are committed."""

    __slots__ = ("_commit_rows", "_committed", "_live", "_max_rows", "_paint")

    def __init__(
        self,
        commit: Callable[[Sequence[str]], None],
        max_rows: int | None = None,
        paint: Callable[[TuiFrame], None] | None = None,
    ) -> None:
        self._commit_rows = commit
        self._paint = paint
        self._max_rows = max_rows
        self._live = TuiFrame()
        self._committed = 0

    # -- queries -----------------------------------------------------------

    def pending_frame(self) -> TuiFrame:
        return self._live

    def committed_rows(self) -> int:
        """Rows handed to scrollback by explicit settled batches."""
        return self._committed

    # -- mutations ---------------------------------------------------------

    def set_max_rows(self, max_rows: int | None) -> None:
        self._max_rows = max_rows

    def set_live(self, frame: TuiFrame) -> None:
        """Keep chrome visible and viewport-trim only unsettled transcript rows."""
        transcript = frame.transcript_rows
        chrome = frame.chrome_rows
        cursor = frame.cursor
        if self._max_rows is not None:
            height = max(0, self._max_rows)
            chrome_dropped = max(0, len(chrome) - height)
            chrome = chrome[-height:] if height else ()
            transcript_budget = max(0, height - len(chrome))
            visible_transcript = transcript[-transcript_budget:] if transcript_budget else ()
            transcript_dropped = len(transcript) - len(visible_transcript)
            transcript = visible_transcript
            if cursor is not None:
                row, column = cursor
                row -= transcript_dropped
                if row < 0:
                    cursor = None
                elif row >= len(transcript):
                    if row < len(transcript) + chrome_dropped:
                        cursor = None
                    else:
                        cursor = (row - chrome_dropped, column)
                else:
                    cursor = (row, column)
                if cursor is not None and cursor[0] >= len(transcript) + len(chrome):
                    cursor = None
        live = TuiFrame(tuple(transcript), tuple(chrome), cursor)
        if live == self._live:
            return
        self._live = live
        if self._paint is not None:
            self._paint(live)

    def commit(self, rows: Sequence[str]) -> None:
        """Write a settle batch once; ``set_live`` never commits anything."""
        settled = tuple(rows)
        if not settled:
            return
        self._commit_rows(settled)
        self._committed += len(settled)

    def clear(self) -> None:
        """Erase the mutable viewport without touching scrollback."""
        if self._live == TuiFrame():
            return
        self._live = TuiFrame()
        if self._paint is not None:
            self._paint(self._live)
