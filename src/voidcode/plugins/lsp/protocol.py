"""LSP wire framing, bounds, and protocol errors.

This is the single stdio transport: JSON-RPC frames are read from the host's
paged process log and written through the supervised stdin pipe. No server
lifecycle or request policy lives here.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Final, Protocol

from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import ProcessServices

from .lsp_types import LspRequestError

MAX_LSP_HEADER_BYTES: Final[int] = 8192
MAX_LSP_MESSAGE_BYTES: Final[int] = 1024 * 1024

_CONTENT_LENGTH_HEADER = "Content-Length"
_CONTENT_LENGTH_PREFIX = _CONTENT_LENGTH_HEADER.lower() + ":"

# The host offers no blocking read on a service process's log, only paged
# polling reads. The framed reader parks on this never-set event between empty
# pages: a bounded blocking wait with backoff, starting below the old fixed
# sleep so quick servers answer sooner and idle waits cost fewer wakeups.
_LSP_POLL_PARK = threading.Event()
_LSP_POLL_MIN_SECONDS = 0.001
_LSP_POLL_MAX_SECONDS = 0.05


class LspRuntimeError(LspRequestError):
    """Base class for runtime-managed LSP failures."""


class LspProtocolError(LspRuntimeError):
    """Raised when an LSP server violates JSON-RPC/LSP framing or payload rules."""


class LspMessageBoundsError(LspRuntimeError):
    """Raised when an LSP message exceeds configured runtime bounds."""


class ServerWireState(Protocol):
    """The per-server wire cursor one framed read consumes and advances."""

    process_id: str
    stdout_cursor: int


def encode_lsp_message(message: dict[str, object]) -> bytes:
    """Encode one LSP stdio frame; every write path goes through here."""
    body = json.dumps(message).encode("utf-8")
    return f"{_CONTENT_LENGTH_HEADER}: {len(body)}\r\n\r\n".encode("ascii") + body


def _park_before_retry(idle_polls: int) -> None:
    _LSP_POLL_PARK.wait(min(_LSP_POLL_MAX_SECONDS, _LSP_POLL_MIN_SECONDS * (2 ** min(idle_polls, 6))))


def read_lsp_frame(
    services: ProcessServices,
    running_server: ServerWireState,
    *,
    server_name: str,
    abort_signal: ProviderAbortSignal | None,
    timeout: float = 30.0,
) -> dict[str, object] | None:
    """Read one LSP stdio frame; the single framed-reader for every request."""
    process_id = running_server.process_id
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    idle_polls = 0

    def _page(needed: int) -> bool:
        nonlocal idle_polls
        if abort_signal is not None and abort_signal.cancelled:
            raise TimeoutError(f"LSP request to {server_name} was cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"LSP server did not respond within {timeout}s")
        page = services.read_process_output(
            process_id,
            stream="stdout",
            offset=running_server.stdout_cursor + len(buffer),
            limit=max(1, min(65536, needed)),
        )
        if page.data:
            buffer.extend(page.data)
            idle_polls = 0
            return True
        if not page.running:
            return False
        _park_before_retry(idle_polls)
        idle_polls += 1
        return True

    while bytes(buffer).find(b"\r\n\r\n") < 0:
        if len(buffer) > MAX_LSP_HEADER_BYTES:
            raise LspMessageBoundsError(f"LSP header exceeded {MAX_LSP_HEADER_BYTES} bytes")
        if not _page(MAX_LSP_HEADER_BYTES + 4 - len(buffer)):
            return None

    header_end = bytes(buffer).find(b"\r\n\r\n")
    header_bytes = bytes(buffer[:header_end])
    if len(header_bytes) > MAX_LSP_HEADER_BYTES:
        raise LspMessageBoundsError(f"LSP header exceeded {MAX_LSP_HEADER_BYTES} bytes")
    try:
        header_text = header_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise LspProtocolError("invalid ASCII header from LSP server") from exc
    content_length = None
    for line in header_text.split("\r\n"):
        if line.lower().startswith(_CONTENT_LENGTH_PREFIX):
            raw_length = line.split(":", 1)[1].strip()
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise LspProtocolError(f"invalid Content-Length from LSP server: {raw_length}") from exc
            break
    if content_length is None:
        raise LspProtocolError("missing Content-Length in LSP response header")
    if content_length < 0:
        raise LspProtocolError("invalid negative Content-Length from LSP server")
    if content_length > MAX_LSP_MESSAGE_BYTES:
        raise LspMessageBoundsError(f"LSP Content-Length {content_length} exceeds {MAX_LSP_MESSAGE_BYTES} bytes")

    body_start = header_end + 4
    while len(buffer) - body_start < content_length:
        if len(buffer) - body_start > MAX_LSP_MESSAGE_BYTES:
            raise LspMessageBoundsError(f"LSP Content-Length {content_length} exceeds {MAX_LSP_MESSAGE_BYTES} bytes")
        if not _page(content_length - (len(buffer) - body_start)):
            return None

    running_server.stdout_cursor += body_start + content_length
    body = bytes(buffer[body_start : body_start + content_length])
    try:
        decoded_body = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LspProtocolError("invalid UTF-8 payload from LSP server") from exc
    try:
        payload = json.loads(decoded_body)
    except json.JSONDecodeError as exc:
        raise LspProtocolError("invalid JSON payload from LSP server") from exc
    if not isinstance(payload, dict):
        raise LspProtocolError("expected JSON-RPC object payload from LSP server")
    return payload
