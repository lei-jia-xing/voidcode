import os
import sys

import pytest
from rich.color import ColorSystem

from voidcode.tui.term import _detect_color_system

if os.name != "posix":
    pytest.skip("PTY termios restoration requires POSIX", allow_module_level=True)

import fcntl
import pty
import struct
import termios

from voidcode.tui.term import Terminal


def test_terminal_restores_real_pty_termios(monkeypatch) -> None:
    master, slave = pty.openpty()
    input_stream = os.fdopen(os.dup(slave), "rb", buffering=0)
    output_stream = os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1)
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    original = termios.tcgetattr(slave)
    terminal = None
    try:
        terminal = Terminal.open()
        assert termios.tcgetattr(slave) != original
    finally:
        if terminal is not None:
            terminal.close()
        restored = termios.tcgetattr(slave)
        input_stream.close()
        output_stream.close()
        os.close(slave)
        os.close(master)

    assert restored == original


@pytest.mark.parametrize(("no_color", "expected"), [(None, ColorSystem.EIGHT_BIT), ("", None), ("1", None)])
def test_any_present_no_color_disables_color(monkeypatch: pytest.MonkeyPatch, no_color: str | None, expected: ColorSystem | None) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("COLORTERM", raising=False)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    assert _detect_color_system(is_tty=True) == expected


def test_non_tty_stays_uncolored_without_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _detect_color_system(is_tty=False) is None
