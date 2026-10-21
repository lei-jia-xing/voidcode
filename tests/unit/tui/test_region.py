from voidcode.core.tui.region import LiveRegion, TuiFrame


def test_resize_trims_only_live_transcript_and_preserves_chrome() -> None:
    committed: list[tuple[str, ...]] = []
    region = LiveRegion(committed.append, max_rows=4)

    region.set_live(TuiFrame(("old", "middle", "new"), ("status", "input")))

    assert region.pending_frame() == TuiFrame(("middle", "new"), ("status", "input"))
    assert region.pending_frame().rows == ("middle", "new", "status", "input")
    assert committed == []

    region.set_max_rows(3)
    region.set_live(TuiFrame(("old", "middle", "new"), ("status", "input")))
    assert region.pending_frame() == TuiFrame(("new",), ("status", "input"))
    assert committed == []


def test_only_explicit_settle_batches_commit_and_cursor_moves_repaint() -> None:
    committed: list[tuple[str, ...]] = []
    painted: list[TuiFrame] = []
    region = LiveRegion(committed.append, paint=painted.append)
    frame = TuiFrame(("draft",), cursor=(0, 0))

    region.set_live(frame)
    region.set_live(frame)
    region.set_live(TuiFrame(("draft",), cursor=(0, 1)))
    assert [item.rows for item in painted] == [("draft",), ("draft",)]
    assert painted[0].cursor == (0, 0)
    assert painted[1].cursor == (0, 1)
    assert committed == []

    region.commit(("settled", "batch"))
    region.commit(("settled", "batch"))
    assert committed == [("settled", "batch"), ("settled", "batch")]
    assert region.committed_rows() == 4
