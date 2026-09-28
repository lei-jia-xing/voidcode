"""Live-region ledger: the mutable frame below the committed scrollback.

Contract (mirrors oh-my-pi's ``HistoryBatch``/viewport split; MIT, ``/tmp/omp-src``):

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
    is never one that would still change.

    The ledger remembers the rows it has actually written and aligns by
    **identity**, not by frame position: a requested write is matched against the
    tail of what was written, the matching prefix is dropped as already in
    scrollback, and the remainder is appended. The guarantee is *"every row the
    region is asked to write reaches scrollback exactly once when the requested
    sequence extends what was written, and never twice by construction; a row is
    never dropped."*

    The two entry points align against different histories, because they answer
    different questions:

    * :meth:`set_live` asks "which of these overflow rows are already in
      scrollback?" -- any row written earlier is, however far back, so it aligns
      against **all** written rows.
    * :meth:`commit` asks "which of this settle batch did the overflow of *the
      same frame* already write?" -- only the overflow handler writes rows a
      settle batch could still be holding, so commit aligns against the rows the
      overflow produced and nothing else. A batch may legitimately repeat text
      written in an earlier turn (the deterministic runtime writes a tool's body
      and the answer with the same string; two turns can answer identically), and
      aligning against committed history would silently drop such a row.

    The tradeoff is the other direction: only a window of the written history is
    remembered (``_WRITE_WINDOW`` rows -- alignment only ever needs the boundary
    region) and scrollback is append-only. If a caller re-lays out rows *above*
    the boundary -- inserting a row over settled rows, or a header whose text
    changed in place -- the ledger cannot rewrite scrollback, so it appends the
    divergent rows. That is the frozen-presentation consequence (the same class
    as omp's "committed rows can never be rewritten"): the old rows stay visible
    where they were and the new rendering follows them. Rows are still never
    dropped.

    The written history survives :meth:`clear` (erasing the live region does not
    erase scrollback) and a resize (the old rows are still there), so an
    over-size frame that comes back at another width writes nothing new.
    :meth:`reset_overflow` is the one legitimate reset: a genuinely new row
    sequence, i.e. the transcript was replaced by a session switch.

    :meth:`commit` consumes rows from *before* the live frame (``take_settled``
    hands out the blocks that used to be its head). The tape joins a settle batch
    with a leading blank separator the live frame never carried, so when that
    blank blocks the overlap the ledger drops it and matches the body -- its place
    (before rows already written) cannot be re-created anyway.

    Content that must be durable belongs in :meth:`commit`; the cap is a safety
    net, not the commit path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

__all__ = ["LiveRegion", "diff_rows"]

#: Rows of written history the ledger keeps for alignment. Only the boundary
#: region is ever needed: a write is matched against the tail of what was written.
_WRITE_WINDOW = 256


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

    __slots__ = ("_commit_rows", "_committed", "_live", "_max_rows", "_overflowed", "_paint", "_written")

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
        # Rows handed to scrollback, oldest first, trimmed to ``_WRITE_WINDOW``.
        self._written: list[str] = []
        # The subset of ``_written`` that the ``set_live`` overflow produced (the
        # only rows a settle batch may legitimately already find in scrollback).
        self._overflowed: list[str] = []
        self._committed = 0

    # -- queries -----------------------------------------------------------

    def pending_rows(self) -> Sequence[str]:
        """Rows currently held live (the frame the terminal should be showing)."""
        return self._live

    def committed_rows(self) -> int:
        """Rows handed to scrollback so far (accounting aid for callers/tests)."""
        return self._committed

    # -- mutations ---------------------------------------------------------

    def set_live(self, rows: Sequence[str]) -> None:
        """Replace the live frame, flushing the overflow and repainting changes.

        Only the rows past the written boundary are new: a frame that grew by a
        row writes that row alone, and an identical frame writes nothing. A row
        whose text changed in place (a spinner, a header that starts counting
        lines) is not matched -- it is appended, which is the frozen case.
        """
        frame = tuple(rows)
        live = frame
        if self._max_rows is not None and len(frame) > self._max_rows:
            split = len(frame) - self._max_rows
            self._append(frame[:split], self._written, self._overflowed)
            live = frame[split:]
        if live == self._live:
            return
        self._live = live
        if self._paint is not None:
            self._paint(live)

    def commit(self, rows: Sequence[str]) -> None:
        """Move ``rows`` into permanent scrollback and out of the live set.

        A settle batch used to be the head of the live frame. The only rows of it
        already in scrollback are the ones the *overflow* wrote, so the batch is
        aligned against those and nothing else -- a batch may legitimately repeat
        text from an earlier turn (the deterministic runtime writes a tool's body
        and the answer with the same string), and matching that would drop a row.
        The tape joins a batch that follows committed rows with a leading blank
        separator the live frame never carried: when that blank blocks the overlap,
        drop it -- the body is the real overlap and the separator's place (before
        rows already written) cannot be re-created.
        """
        frame = tuple(rows)
        if frame[:1] == ("",) and self._overlap(frame[1:], self._overflowed) > 0:
            self._append(frame[1:], self._overflowed, None)
        else:
            self._append(frame, self._overflowed, None)
        if self._live[: len(frame)] == frame:
            self._live = self._live[len(frame) :]

    def clear(self) -> None:
        """Drop the live set and erase it from the terminal.

        Only the *paint* state goes. The written history survives: erasing the
        live region does not erase the scrollback, so rows already written stay
        matched and cannot be written a second time.
        """
        if not self._live:
            return
        self._live = ()
        if self._paint is not None:
            self._paint(())

    def reset_overflow(self) -> None:
        """Forget the written history: the row sequence is genuinely new.

        The app calls this on a session switch, where the transcript is replaced
        and the new frame's rows have nothing to do with the old ones. A resize
        must NOT call it -- after a width change the old rows are still in
        scrollback, so the ledger must keep matching against them.
        """
        self._written.clear()
        self._overflowed.clear()

    # -- internals ---------------------------------------------------------

    def _append(self, desired: Sequence[str], base: Sequence[str], also: list[str] | None) -> None:
        """Append ``desired``, writing only rows not already the tail of ``base``.

        ``k`` counts the leading rows of ``desired`` already the tail of ``base``:
        those are the same rows in scrollback, so re-writing them would duplicate
        them. Everything past ``k`` is new content and must be written; no row in
        ``desired`` is ever skipped, so this cannot lose a row. ``also`` (when
        given) records the new rows in a second history (``_overflowed``).
        """
        new = desired[self._overlap(desired, base) :]
        if not new:
            return
        self._commit_rows(new)
        self._committed += len(new)
        for history in (self._written, also):
            if history is None:
                continue
            history.extend(new)
            del history[: max(0, len(history) - _WRITE_WINDOW)]

    def _overlap(self, desired: Sequence[str], base: Sequence[str]) -> int:
        """Largest ``k`` with ``base[-k:] == desired[:k]`` (0 with no overlap)."""
        for k in range(min(len(base), len(desired)), 0, -1):
            if list(base[-k:]) == list(desired[:k]):
                return k
        return 0
