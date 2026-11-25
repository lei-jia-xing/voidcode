"""Bounded log scanning and readiness polling for background processes."""

from __future__ import annotations

import time
from dataclasses import dataclass

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ProcessServices

from . import background_limits as _limits
from .background_models import _StreamName


@dataclass(frozen=True, slots=True)
class _StreamScan:
    """One stream's log lines as read through the host's bounded byte windows."""

    lines: list[str]
    truncated: bool
    bytes_scanned: int


def _scan_stream(services: ProcessServices, process_id: str, stream: _StreamName) -> _StreamScan:
    """Every line of one stream's log, read out of the host's bounded byte windows.

    Lines retain endings with CRLF normalized to LF. The scan stops at a byte
    ceiling: a runaway log costs a bounded read, and the caller is told `truncated` rather than
    handed a silently short page. `bytes_scanned` is the honest byte cursor for the next read.
    """
    chunks: list[bytes] = []
    offset = 0
    while True:
        remaining = _limits._LOG_SCAN_BYTES - offset
        if remaining <= 0:
            text = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
            return _StreamScan(lines=text.splitlines(keepends=True), truncated=True, bytes_scanned=offset)
        page = services.read_process_output(process_id, stream=stream, offset=offset, limit=min(_limits._LOG_PAGE_BYTES, remaining))
        offset += len(page.data)
        chunks.append(page.data)
        if page.eof or not page.data:
            text = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
            return _StreamScan(lines=text.splitlines(keepends=True), truncated=not page.eof, bytes_scanned=offset)


def _wait_for_ready(services: ProcessServices, process_id: str, *, pattern: str | None, timeout: float, context: ToolContext) -> bool | None:
    """Whether the process printed `pattern`; `None` when not requested or waiting was cancelled.

    Both streams are polled from the byte the last poll consumed, keeping the legacy tail window,
    and a process that exits first ends the wait without waiting out the whole timeout.
    """
    if pattern is None:
        return None
    deadline = time.monotonic() + timeout
    offsets = {"stdout": 0, "stderr": 0}
    scanned = {"stdout": b"", "stderr": b""}
    while True:
        running = False
        drained = True
        for stream in ("stdout", "stderr"):
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return None
            page = services.read_process_output(process_id, stream=stream, offset=offsets[stream], limit=_limits._LOG_PAGE_BYTES)
            offsets[stream] += len(page.data)
            running = running or page.running
            drained = drained and page.eof
            if not page.data:
                continue
            window = scanned[stream] + page.data
            scanned[stream] = window[-_limits._READY_SCAN_BYTES :]
            text = window.decode("utf-8", errors="replace").replace("\r\n", "\n")
            if any(pattern in line for line in text.splitlines()):
                return True
        if (not running and drained) or time.monotonic() >= deadline:
            return False
        time.sleep(_limits._READY_POLL_SECONDS)
