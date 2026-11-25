"""Bounds and polling policy for the background process tool."""

from __future__ import annotations

from voidcode.sdk.tools import TOOL_OUTPUT_BYTES

#: Row bound of one `ps` listing, as the legacy command had.
MAX_PROCESS_ROWS = 64
#: One `send` payload, including the optional newline; larger input is refused before it is written.
MAX_SEND_BYTES = 64 * 1024
#: Byte window of one log read, inside the host's own page bound.
_LOG_PAGE_BYTES = 64 * 1024
#: Ceiling on the bytes one `logs` call scans, so a runaway log costs a bounded read.
_LOG_SCAN_BYTES = 8 * 1024 * 1024
#: Model-visible line cap, matching the read tool's display policy.
_LOG_LINE_CHARS = 2000
_LOG_LINE_SUFFIX = f"... (line truncated to {_LOG_LINE_CHARS} chars)"
#: Model-visible content budget; the rest of a large page stays available at its next offset.
_LOG_RENDER_BYTES = TOOL_OUTPUT_BYTES - 4096
#: Tail window of both streams scanned for the ready pattern, as the legacy wait scanned.
_READY_SCAN_BYTES = 64 * 1024
#: Poll interval of the ready wait, as the legacy manager polled.
_READY_POLL_SECONDS = 0.03
