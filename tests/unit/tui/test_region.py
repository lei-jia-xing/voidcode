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
