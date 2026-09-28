from __future__ import annotations

from voidcode.tui.region import LiveRegion, diff_rows


def test_diff_rows_reports_only_changed_and_new_rows() -> None:
    previous = ["a", "b", "c"]
    assert diff_rows(previous, previous) == ()
    assert diff_rows(previous, ["a", "B", "c"]) == ((1, "B"),)
    assert diff_rows(previous, ["a"]) == ()
    assert diff_rows(previous, ["a", "b", "c", "d"]) == ((3, "d"),)
    assert diff_rows([], ["only"]) == ((0, "only"),)


def test_second_render_of_the_same_live_frame_paints_nothing() -> None:
    committed: list[tuple[str, ...]] = []
    painted: list[tuple[str, ...]] = []
    region = LiveRegion(committed.append, max_rows=4, paint=painted.append)

    assert painted == []
    region.set_live(["a", "b"])
    assert painted == [("a", "b")]
    region.set_live(["a", "b"])
    assert painted == [("a", "b")]
    region.set_live(["a", "c"])
    assert painted == [("a", "b"), ("a", "c")]


def test_committed_rows_leave_the_live_set_permanently() -> None:
    committed: list[tuple[str, ...]] = []
    painted: list[tuple[str, ...]] = []
    region = LiveRegion(committed.append, max_rows=8, paint=painted.append)

    region.commit(["USER-1", "", "ASSISTANT-1"])
    region.set_live(["tool card", "status"])
    assert region.pending_rows() == ("tool card", "status")
    assert region.committed_rows() == 3

    region.commit(["ASSISTANT-2", ""])
    assert "ASSISTANT-2" not in region.pending_rows()
    assert committed == [("USER-1", "", "ASSISTANT-1"), ("ASSISTANT-2", "")]


def test_committing_a_live_prefix_removes_it_from_the_live_set() -> None:
    region = LiveRegion(commit=lambda rows: None, max_rows=8, paint=lambda rows: None)
    region.set_live(["keep", "settled", "tail"])
    region.commit(["keep", "settled"])
    assert region.pending_rows() == ("tail",)


def test_overflow_flushes_the_oldest_rows_into_scrollback() -> None:
    committed: list[tuple[str, ...]] = []
    painted: list[tuple[str, ...]] = []
    region = LiveRegion(committed.append, max_rows=3, paint=painted.append)

    region.set_live(["old-1", "old-2", "live-1", "live-2", "live-3"])
    assert committed == [("old-1", "old-2")]
    assert painted == [("live-1", "live-2", "live-3")]
    assert region.pending_rows() == ("live-1", "live-2", "live-3")

    # Repeating the identical over-size frame must not re-commit anything.
    region.set_live(["old-1", "old-2", "live-1", "live-2", "live-3"])
    assert committed == [("old-1", "old-2")]
    assert len(painted) == 1


def test_a_growing_frame_commits_each_overflow_row_exactly_once() -> None:
    """A streaming tail is the growing-frame case: only the new rows are committed."""
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=5)

    # 4 -> 9 rows, one at a time: overflow grows by one row per step.
    for count in range(4, 10):
        region.set_live([f"line {index}" for index in range(count)])

    # Rows 0-3 overflowed (frames of 6..9 rows); the trailing 5 rows stay live.
    assert committed == ["line 0", "line 1", "line 2", "line 3"], committed
    assert len(committed) == len(set(committed))
    assert region.pending_rows() == ("line 4", "line 5", "line 6", "line 7", "line 8")


def test_a_mutating_flushed_head_is_not_re_written() -> None:
    """A spinner/header row changes text in place; it is the same scrollback row.

    The mutated head is not the row the ledger would write next, so the rows
    behind it are written once each and the head is left as it was committed --
    scrollback is append-only and the old text stays visible there.
    """
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=2)

    region.set_live(["H1", "a", "b"])  # write ["H1"]
    region.set_live(["H1", "a", "b", "c"])  # write ["a"]
    region.set_live(["H1", "a", "b", "c", "d"])  # write ["b"]

    assert committed == ["H1", "a", "b"], committed
    assert len(committed) == len(set(committed))
    assert region.pending_rows() == ("c", "d")


def test_a_row_that_became_the_head_after_a_shrink_is_not_lost() -> None:
    """The verifier's loss counterexample: shrink, regrow, then settle.

    The retained written history is longer than the new overflow, so a
    position-count rule drops the row that became the head. Identity alignment
    writes it, because it is not among the rows already written.
    """
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=3)

    region.set_live(["H", "a", "b", "c"])  # write ["H"]
    region.set_live(["a", "b"])  # shrank below the cap
    region.set_live(["a", "b", "c", "d"])  # over the cap again
    region.commit(["a", "b", "c", "d"])  # the turn settles

    # ``a`` was only ever live; it must reach scrollback exactly once.
    assert committed == ["H", "a", "b", "c", "d"], committed
    assert "a" in committed
    assert len(committed) == len(set(committed))


def test_clear_keeps_the_written_history_so_a_refill_is_not_re_written() -> None:
    """Erasing the live region does not erase scrollback, so the ledger survives."""
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=3)

    frame = ["H", "a", "b", "c"]
    region.set_live(frame)  # write ["H"]
    region.clear()  # ``_apply_resize`` / alt-screen borrow
    region.set_live(frame)  # same rows, different width: nothing is new

    assert committed == ["H"], committed


def test_commit_does_not_skip_a_row_that_was_only_ever_live() -> None:
    """A settle batch writes its rows even when it extends live rows never flushed.

    The overflow wrote nothing yet (the frame fit), so the whole batch is new and
    must reach scrollback; skipping its head would lose a row.
    """
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=8)

    region.set_live(["a", "b", "c"])
    region.commit(["a", "b", "c"])

    assert committed == ["a", "b", "c"], committed
    assert region.pending_rows() == ()


def test_resize_reflow_does_not_re_write_the_overflow() -> None:
    """A narrower terminal then a wider one must not re-write the old prefix."""
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=10)
    tall = [f"line {index}" for index in range(20)]

    region.set_live(tall)  # write the leading 10
    region.clear()  # resize
    region.set_live(tall[:10])  # narrow: no overflow now
    region.set_live(tall)  # wide again: the first 10 are already in scrollback

    assert committed == [f"line {index}" for index in range(10)], committed
    assert len(committed) == len(set(committed))


def test_reset_overflow_is_the_only_way_to_forget_the_written_rows() -> None:
    """A session switch replaces the row sequence; only then may rows re-write."""
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=3)

    region.set_live(["H", "a", "b", "c"])
    region.reset_overflow()  # session switch
    region.set_live(["H", "a", "b", "c"])
    assert committed == ["H", "H"]

    region.reset_overflow()
    region.set_live(["H", "a", "b", "c"])
    assert committed == ["H", "H", "H"]


def test_a_batch_that_repeats_an_earlier_turns_text_is_still_written() -> None:
    """Two turns can answer with the same text; the second is not a duplicate.

    The deterministic runtime writes a tool's body and the answer with the same
    string, so aligning a settle batch against *all* written history would drop
    the second turn's answer. ``commit`` therefore aligns against only what the
    current frame wrote.
    """
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=30)

    region.set_live(["✔ read", "FILE-1"])
    region.commit(["✔ read", "FILE-1"])  # turn 1 settles
    region.set_live(["✔ read", "FILE-1"])
    region.commit(["", "✔ read", "FILE-1"])  # turn 2 repeats the string
    region.commit(["", "FILE-1"])  # the answer row settles after the tool card

    assert committed.count("FILE-1") == 3, committed  # tool body + answer + turn 2's


def test_commit_does_not_re_write_what_the_overflow_already_wrote() -> None:
    """The app's settle path hands the whole block, head included, to ``commit``."""
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=3)

    # A live frame of 4 overflows its head row into scrollback...
    region.set_live(["p0", "p1", "p2", "p3"])
    # ...and the turn then settles, handing the full block to ``commit``.
    region.commit(["p0", "p1", "p2"])

    assert committed == ["p0", "p1", "p2"], committed
    assert region.committed_rows() == 3


def test_a_settle_batch_drops_its_leading_separator_when_it_blocks_the_overlap() -> None:
    """The tape joins a batch with a blank the live frame never carried.

    When that blank blocks the overlap, the body is the real overlap: the blank is
    dropped and no row is re-written or lost.
    """
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=3)

    region.set_live(["H", "a", "b", "c"])  # write ["H"]
    region.commit(["", "H", "a", "b"])  # a batch after committed rows

    assert committed == ["H", "a", "b"], committed
    assert len(committed) == len(set(committed))


def test_head_insertion_above_the_boundary_appends_the_divergent_rows() -> None:
    """The documented frozen case: a row inserted above the written boundary.

    Scrollback is append-only, so the ledger cannot shift the written prefix. It
    appends the rows it is asked to write that it does not already hold -- the
    frozen-presentation tradeoff. The important half: **no row is lost**.
    """
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=2)

    region.set_live(["A", "B", "P"])  # write ["A"]
    region.set_live(["A", "B", "P", "Q"])  # write ["B"]
    region.set_live(["A", "N", "B", "P", "Q"])  # N inserted above the boundary

    # The ledger appends what it can; ``N`` (divergent) follows the written prefix,
    # and the rows that never left the live tail (``P``/``Q``) are still live. What
    # must never happen is a row in neither place.
    delivered = set(committed) | set(region.pending_rows())
    for row in ("A", "B", "N", "P", "Q"):
        assert row in delivered, (row, committed, region.pending_rows())


def test_the_verifier_region_counterexamples_lose_no_row() -> None:
    """Every row a settle batch hands the ledger reaches scrollback at least once.

    The exact scripts from the round-2 report (``min_region.py``,
    ``case_A_B_C.py``). A duplicate in a head-insertion script is the documented
    frozen case -- a row inserted above the written boundary cannot be inserted
    into append-only scrollback -- but a *lost* row never is.
    """

    def run(script: list[tuple[str, list[str]]], max_rows: int) -> list[str]:
        committed: list[str] = []
        region = LiveRegion(lambda rows: committed.extend(rows), max_rows=max_rows)
        for op, arg in script:
            getattr(region, op)(arg)
        return committed

    scripts: list[tuple[int, list[tuple[str, list[str]]]]] = [
        # min_region: head insertion, then the turn settles
        (3, [("set_live", ["A", "B", "P", "Q"]), ("set_live", ["A", "N", "B", "P", "Q"]), ("commit", ["A", "N", "B", "P", "Q"])]),
        # case_A_B_C B: shrink, regrow, then settle
        (3, [("set_live", ["H", "a", "b", "c"]), ("set_live", ["a", "b"]), ("set_live", ["a", "b", "c", "d"]), ("commit", ["a", "b", "c", "d"])]),
        # case_A_B_C C: a row appears above the boundary, then the batch settles
        (
            3,
            [
                ("set_live", ["H", "a", "b", "c"]),
                ("set_live", ["X", "H", "a", "b", "c"]),
                ("set_live", ["X", "H", "a", "b", "c", "d"]),
                ("commit", ["X", "H", "a", "b", "c", "d"]),
            ],
        ),
    ]
    for max_rows, script in scripts:
        committed = run(script, max_rows)
        settled = next(arg for op, arg in script if op == "commit")
        for row in settled:
            assert row in committed, (row, committed)


def test_a_batch_whose_head_is_not_the_overflowed_row_may_re_append_it() -> None:
    """A settle batch that opens with a row the overflow never wrote re-appends.

    The ledger detects its overlap only at the *start* of the batch: it matches
    the batch's leading rows against the rows the overflow wrote. When the head
    is a different row (here the batch is re-ordered -- notices first, then the
    settled ``A``), the overlap is 0 and the already-written rows behind that
    head are appended again rather than risk dropping a row. Append-only
    scrollback cannot be re-ordered, so a duplicate is the honest outcome.

    Pinned deliberately: the duplicate is the documented residual, not a bug, and
    the half that must never change is that **no row of the batch is lost**.
    """
    committed: list[str] = []
    region = LiveRegion(lambda rows: committed.extend(rows), max_rows=2)

    region.set_live(["A", "B", "C"])  # exactly max_rows: nothing overflows yet
    region.set_live(["A", "B", "C", "D"])  # split=1 -> writes ["A"]
    region.commit(["B", "C", "D", "A"])  # the batch head is "B", not "A"

    assert committed.count("A") == 2, committed  # the residual duplicate
    assert len(committed) > len(set(committed)), "the duplicate must be deliberate, not optimised away"
    # No loss: every distinct row of the batch reached scrollback.
    for row in ("A", "B", "C", "D"):
        assert row in committed, (row, committed)


def test_unbounded_region_keeps_every_row_live() -> None:
    region = LiveRegion(commit=lambda rows: None, paint=lambda rows: None)
    rows = [f"row-{index}" for index in range(40)]
    region.set_live(rows)
    assert list(region.pending_rows()) == rows


def test_clear_drops_the_live_set_and_erases_it() -> None:
    painted: list[tuple[str, ...]] = []
    region = LiveRegion(commit=lambda rows: None, max_rows=4, paint=painted.append)
    region.set_live(["a", "b"])
    region.clear()
    assert region.pending_rows() == ()
    assert painted[-1] == ()
    region.set_live(["c"])
    assert painted[-1] == ("c",)
