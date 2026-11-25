"""OSC 52 clipboard text output for the terminal client."""

from __future__ import annotations

import base64
import os
import sys

__all__ = ["OSC52_MAX_RAW_BYTES", "copy_to_clipboard"]

#: Max raw bytes base64-encoded into one OSC 52 sequence (Codex parity).
OSC52_MAX_RAW_BYTES = 100_000


def copy_to_clipboard(text: str, *, tmux: bool | None = None) -> None:
    """Write ``text`` to the system clipboard via OSC 52 (Codex ``osc52_copy`` parity).

    Emitted on stdout so it reaches the terminal emulator even over SSH; the
    tmux DCS wrapper is applied inside tmux (auto-detected via ``$TMUX``).
    Raises ``ValueError`` when the payload exceeds ``OSC52_MAX_RAW_BYTES`` and
    ``OSError`` when stdout is unavailable.
    """
    raw = text.encode("utf-8")
    if len(raw) > OSC52_MAX_RAW_BYTES:
        raise ValueError(f"clipboard payload exceeds {OSC52_MAX_RAW_BYTES} bytes")
    payload = base64.b64encode(raw).decode("ascii")
    sequence = f"\x1b]52;c;{payload}\x07"
    inside_tmux = os.environ.get("TMUX") is not None if tmux is None else tmux
    if inside_tmux:
        sequence = f"\x1bPtmux;\x1b{sequence}\x1b\\"
    sys.stdout.write(sequence)
    sys.stdout.flush()
