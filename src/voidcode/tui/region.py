"""Live-region ledger: the mutable frame below the committed scrollback.

Contract (mirrors oh-my-pi's history/viewport split, see
``docs/tui-core-renderer.md`` invariants 1-3 in the omp checkout):

* Rows handed to :meth:`LiveRegion.commit` leave the live set **permanently**.
  They are written once into native scrollback by the commit callback and are
  never part of a later live frame.
* :meth:`LiveRegion.set_live` takes the *complete* desired live frame, so the
  caller never has to compute a delta: identical frames are dropped here, and
  the per-row diff of the changed rows lives in :func:`diff_rows` (used by the
  terminal's repaint).

Overflow policy (decided here, and the only place it is decided):

    The live region is capped at ``max_rows`` (the terminal height). When a
    requested frame is taller, the **oldest** rows -- the leading overflow --
    are flushed into scrollback through the commit callback and only the
    trailing ``max_rows`` rows stay live. The trailing end is the part that
    must stay on screen (status line, composer, streaming tail), and omp's
    grammar keeps every animated glyph in the trailing rows, so a flushed row
    is never one that would still change. A repeated identical over-size
    request does not re-flush: the overflow already committed for the current
    request sequence is remembered.

    Content that must be durable belongs in :meth:`commit`; the cap is a safety
    net, not the commit path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

__all__ = ["LiveRegion", "diff_rows"]


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
    """Ledger of the rows that are still allowed to change.

    ``commit`` writes rows into permanent scrollback, ``paint`` repaints the
    live frame (omit it for a headless ledger). ``max_rows`` bounds the live
    region; ``None`` means unbounded.
    """

    __slots__ = ("_commit_rows", "_committed", "_flushed", "_live", "_max_rows", "_paint")

    def __init__(
        self,
        commit: Callable[[Sequence[str]], None],
        max_rows: int | None = None,
        paint: Callable[[Sequence[str]], None] | None = None,
    ) -> None:
        self._commit_rows = commit
        self._paint = paint
        self._max_rows = max_rows
        self._live: tuple[str, ...] = ()
        self._flushed: tuple[str, ...] = ()
        self._committed = 0

    # -- queries -----------------------------------------------------------

    def pending_rows(self) -> Sequence[str]:
        """Rows currently held live (the frame the terminal should be showing)."""
        return self._live

    def committed_rows(self) -> int:
        """Number of rows handed to scrollback (accounting aid for callers/tests)."""
        return self._committed

    # -- mutations ---------------------------------------------------------

    def set_live(self, rows: Sequence[str]) -> None:
        """Replace the live frame, flushing the overflow and repainting changes."""
        frame = tuple(rows)
        live = frame
        if self._max_rows is not None and len(frame) > self._max_rows:
            split = len(frame) - self._max_rows
            overflow, live = frame[:split], frame[split:]
            if overflow != self._flushed:
                self.commit(overflow)
                self._flushed = overflow
        else:
            self._flushed = ()
        if live == self._live:
            return
        self._live = live
        if self._paint is not None:
            self._paint(live)

    def commit(self, rows: Sequence[str]) -> None:
        """Move ``rows`` into permanent scrollback and out of the live set."""
        frame = tuple(rows)
        if not frame:
            return
        self._commit_rows(frame)
        self._committed += len(frame)
        if self._live[: len(frame)] == frame:
            self._live = self._live[len(frame) :]

    def clear(self) -> None:
        """Drop the live set and erase it from the terminal."""
        self._flushed = ()
        if not self._live:
            return
        self._live = ()
        if self._paint is not None:
            self._paint(())
