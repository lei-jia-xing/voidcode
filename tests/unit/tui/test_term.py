from __future__ import annotations

import signal
from collections.abc import Iterator

import pytest

from voidcode.tui import term as term_module
from voidcode.tui.term import (
    PAINT_BEGIN,
    Terminal,
    clamp_row,
    render_lines,
    visible_width,
    wrap_row,
)

WIDTH = 80
HEIGHT = 24


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Capture the exact bytes the terminal writes, without a real tty."""
    chunks: list[str] = []

    def fake_write(fd: int, data: bytes) -> int:
        chunks.append(data.decode("utf-8"))
        return len(data)

    monkeypatch.setattr(term_module.os, "write", fake_write)
    yield chunks


def make_terminal(*, is_tty: bool = True, width: int = WIDTH, height: int = HEIGHT) -> Terminal:
    return Terminal(output_fd=1, is_tty=is_tty, size=(width, height))


# ---------------------------------------------------------------------------
# Width layer
# ---------------------------------------------------------------------------


def test_visible_width_ignores_ansi_and_counts_cells() -> None:
    assert visible_width("abc") == 3
    assert visible_width("\x1b[31mred\x1b[0m") == 3
    assert visible_width("你好") == 4
    assert visible_width("a🎉b") == 4
    assert visible_width("\x1b]0;title\x07xy") == 2


def test_clamp_row_never_exceeds_width_with_wide_chars() -> None:
    assert visible_width(clamp_row("你好世界", 5)) <= 5
    assert clamp_row("你好世界", 5) == "你好"
    assert clamp_row("🎉🎉🎉", 3) == "🎉"
    assert clamp_row("abcdef", 0) == ""


def test_clamp_row_keeps_styling_and_closes_it() -> None:
    styled = "\x1b[31mabcdef\x1b[0m"
    clamped = clamp_row(styled, 3)
    assert visible_width(clamped) == 3
    assert clamped == "\x1b[31mabc\x1b[0m"


def test_clamp_row_fast_path_returns_identical_string() -> None:
    plain = "short row"
    assert clamp_row(plain, 80) is plain


def test_render_lines_wraps_and_fits() -> None:
    rows = render_lines("x" * 30, 10)
    assert len(rows) == 3
    assert all(visible_width(row) <= 10 for row in rows)


def test_wrap_row_preserves_ansi_across_the_break() -> None:
    rows = wrap_row("\x1b[31m" + "word " * 6 + "\x1b[0m", 12)
    assert len(rows) > 1
    assert all(visible_width(row) <= 12 for row in rows)
    assert all("\x1b[31m" in row for row in rows)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def test_commit_rows_writes_rows_once_and_brackets_the_write(emitted: list[str]) -> None:
    terminal = make_terminal()
    terminal.commit_rows(["COMMIT-A", "COMMIT-B"])
    first = "".join(emitted)
    assert first.count("COMMIT-A") == 1
    assert first.startswith(PAINT_BEGIN)
    assert first.count("\x1b[?2026h") == first.count("\x1b[?2026l") == 1

    emitted.clear()
    terminal.commit_rows(["COMMIT-C"])
    second = "".join(emitted)
    assert "COMMIT-A" not in second
    assert second.count("COMMIT-C") == 1


def test_paint_frame_is_empty_when_nothing_changed(emitted: list[str]) -> None:
    terminal = make_terminal()
    frame = ["LIVE-STATIC-A", "LIVE-STATIC-B", "TICK-000"]
    terminal.paint_frame(frame)
    assert emitted
    emitted.clear()
    terminal.paint_frame(frame)
    assert emitted == []
    terminal.paint_frame([*frame[:2], "TICK-001"])
    painted = "".join(emitted)
    assert painted.count("TICK-001") == 1
    assert "LIVE-STATIC-A" not in painted


def test_paint_frame_shrinking_erases_the_stale_tail(emitted: list[str]) -> None:
    terminal = make_terminal()
    terminal.paint_frame(["row-0", "row-1", "row-2"])
    emitted.clear()
    terminal.paint_frame(["row-0"])
    painted = "".join(emitted)
    assert "\x1b[J" in painted
    assert "row-1" not in painted


def test_committed_rows_never_reenter_a_live_frame(emitted: list[str]) -> None:
    terminal = make_terminal()
    terminal.commit_rows([f"COMMIT-{index:03d}" for index in range(20)])
    for frame in range(5):
        terminal.paint_frame(["LIVE-A", f"TICK-{frame}"])
    stream = "".join(emitted)
    assert all(stream.count(f"COMMIT-{index:03d}") == 1 for index in range(20))
    assert stream.count("LIVE-A") == 1


def test_paint_frame_clamps_rows_to_the_terminal_width(emitted: list[str]) -> None:
    terminal = make_terminal(width=20)
    terminal.paint_frame(["W" * 60, "short"])
    painted = "".join(emitted)
    assert "W" * 20 in painted
    assert "W" * 21 not in painted


def test_close_erases_live_region_and_restores_protocols(emitted: list[str]) -> None:
    terminal = make_terminal()
    terminal.paint_frame(["LIVE-ROW"])
    emitted.clear()
    terminal.close()
    closing = "".join(emitted)
    assert "\x1b[J" in closing
    assert "\x1b[?2004l" in closing
    assert "\x1b[<u" in closing
    assert "\x1b[?25h" in closing
    assert closing.count("\x1b[?2026h") == closing.count("\x1b[?2026l")


def test_resize_flag_is_reported_then_cleared_by_the_next_frame(emitted: list[str]) -> None:
    terminal = make_terminal()
    terminal.paint_frame(["LIVE-ROW"])
    assert terminal.resize_pending() is False

    terminal._on_winch(signal.SIGWINCH, None)  # the real SIGWINCH entry point
    assert terminal.resize_pending() is True
    emitted.clear()
    terminal.paint_frame(["LIVE-ROW"])
    assert terminal.resize_pending() is False
    assert "\x1b[J" in "".join(emitted)  # stale geometry forced a full repaint


# ---------------------------------------------------------------------------
# Alternate screen borrow
# ---------------------------------------------------------------------------


def test_alt_screen_borrow_is_balanced_and_forces_a_repaint(emitted: list[str]) -> None:
    terminal = make_terminal()
    terminal.paint_frame(["LIVE-ROW"])

    emitted.clear()
    terminal.enter_alt_screen()
    assert terminal.alt_screen is True
    terminal.paint_frame(["PICKER-ROW"])
    assert "PICKER-ROW" in "".join(emitted)

    emitted.clear()
    terminal.leave_alt_screen()
    assert terminal.alt_screen is False
    assert terminal.resize_pending() is True  # normal-buffer live region must be repainted
    stream = "".join(emitted)
    assert stream.count("\x1b[?1049h") == 0
    assert "\x1b[?1049l" in stream


def test_enter_alt_screen_is_idempotent_and_leave_without_borrow_writes_nothing(emitted: list[str]) -> None:
    terminal = make_terminal()
    emitted.clear()
    terminal.leave_alt_screen()
    assert "".join(emitted) == ""

    terminal.enter_alt_screen()
    terminal.enter_alt_screen()
    assert "".join(emitted).count("\x1b[?1049h") == 1


def test_non_tty_terminal_never_enters_the_alt_screen(emitted: list[str]) -> None:
    terminal = make_terminal(is_tty=False)
    terminal.enter_alt_screen()
    assert terminal.alt_screen is False
    assert "".join(emitted) == ""


def test_close_releases_a_borrowed_alt_screen(emitted: list[str]) -> None:
    terminal = make_terminal()
    terminal.enter_alt_screen()
    emitted.clear()
    terminal.close()
    assert "\x1b[?1049l" in "".join(emitted)


# ---------------------------------------------------------------------------
# Degraded mode
# ---------------------------------------------------------------------------


def test_non_tty_terminal_writes_plain_lines_and_reads_nothing(emitted: list[str]) -> None:
    terminal = make_terminal(is_tty=False)
    terminal.commit_rows(["plain row"])
    terminal.paint_frame(["live a", "live b"])
    stream = "".join(emitted)
    assert "\x1b" not in stream
    assert stream == "plain row\nlive a\nlive b\n"
    assert terminal.read_bytes(0) == b""
    assert terminal.resize_pending() is False


def test_has_input_reports_whether_any_read_can_succeed() -> None:
    assert make_terminal().has_input is False  # no input_fd was configured
    with_input = Terminal(input_fd=0, output_fd=1, is_tty=False)
    assert with_input.has_input is True
    with_input._eof = True
    assert with_input.has_input is False
