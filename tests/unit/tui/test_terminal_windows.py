import ctypes
import os
import sys
from ctypes import wintypes

import pytest

from voidcode.core.tui.term import Terminal


def test_windows_console_modes_are_restored() -> None:
    if os.name != "nt":
        pytest.skip("requires a Windows console")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        pytest.skip("requires an attached Windows console")

    msvcrt = __import__("msvcrt")
    kernel32 = getattr(ctypes, "Win" + "DLL")("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL

    def console_mode(fd: int) -> int:
        handle = getattr(msvcrt, "get_" + "osfhandle")(fd)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            raise OSError(getattr(ctypes, "get_" + "last_error")(), "GetConsoleMode failed")
        return mode.value

    fds = (sys.stdin.fileno(), sys.stdout.fileno())
    before = tuple(console_mode(fd) for fd in fds)
    terminal = Terminal.open()
    try:
        active_input, active_output = (console_mode(fd) for fd in fds)
        assert active_input != before[0]
        assert active_input & 0x0007 == 0
        assert active_input & 0x0200
        assert active_output & 0x0004
        assert terminal.is_tty
    finally:
        terminal.close()

    assert tuple(console_mode(fd) for fd in fds) == before
