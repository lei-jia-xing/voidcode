"""Kitty reply routing: a correlated reply never swallows adjacent input."""

from __future__ import annotations

import socket

from voidcode.tui.kitty_graphics import KITTY_PROBE_ID
from voidcode.tui.term import Terminal


def _probe_reply(message: str = "OK") -> bytes:
    return b"\x1b_Gi=" + str(KITTY_PROBE_ID).encode("ascii") + b";" + message.encode("ascii") + b"\x1b\\"


def test_probe_reply_beside_planted_input_is_routed_not_swallowed() -> None:
    terminal = Terminal()
    terminal._kitty_probe_pending = True
    passthrough = terminal._filter_kitty_replies(b"kept" + _probe_reply() + b"more")
    assert passthrough == b"keptmore"
    assert terminal._kitty_probe_pending is False
    assert terminal.kitty_graphics_supported is True


def test_graphics_probe_runs_once_on_first_support_check() -> None:
    inbound, feeder = socket.socketpair()
    outbound, drain = socket.socketpair()
    try:
        feeder.sendall(_probe_reply())
        terminal = Terminal(input_fd=inbound.fileno(), output_fd=outbound.fileno(), is_tty=True)
        assert terminal._kitty_probed is False
        assert terminal.kitty_graphics_supported is True
        first = drain.recv(65536)
        assert first.count(b"\x1b_G") == 1
        assert str(KITTY_PROBE_ID).encode("ascii") in first
        assert terminal.kitty_graphics_supported is True
        drain.setblocking(False)
        try:
            extra = drain.recv(65536)
        except BlockingIOError:
            extra = b""
        assert extra == b""
    finally:
        inbound.close()
        feeder.close()
        outbound.close()
        drain.close()
