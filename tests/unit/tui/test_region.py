from voidcode.core.tui.region import TuiFrame, diff_rows


def test_tui_frame_properties() -> None:
    frame = TuiFrame(("line1", "line2"), ("status", "input"), cursor=(1, 5))
    assert frame.transcript_rows == ("line1", "line2")
    assert frame.chrome_rows == ("status", "input")
    assert frame.cursor == (1, 5)
    assert frame.rows == ("line1", "line2", "status", "input")


def test_diff_rows_finds_changes_and_additions() -> None:
    prev = ["one", "two", "three"]
    curr = ["one", "MODIFIED", "three", "four"]
    assert diff_rows(prev, curr) == ((1, "MODIFIED"), (3, "four"))


def test_diff_rows_identical_returns_empty() -> None:
    rows = ["one", "two"]
    assert diff_rows(rows, rows) == ()
