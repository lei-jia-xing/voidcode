import os
import sys

import pytest

if os.name != "posix":
    pytest.skip("PTY termios restoration requires POSIX", allow_module_level=True)

import fcntl
import pty
import struct
import termios

from voidcode.core.tui.term import Terminal


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
