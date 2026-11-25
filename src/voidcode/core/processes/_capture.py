"""Bounded stream capture and the reader/writer threads for one-shot runs."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from voidcode.sdk.services import ProcessStream

#: Hard ceiling for one stream's capture. The bound exists so a runaway command cannot exhaust the
#: core's memory; a caller that wants more must spill the output itself.
MAX_CAPTURE_BYTES: Final[int] = 256 * 1024 * 1024
_READ_CHUNK_BYTES: Final[int] = 8192


@dataclass(slots=True)
class _StreamCapture:
    """Bounded in-memory capture; excess bytes are dropped and reported.

    The lock lets the result path snapshot a prefix while a stalled reader thread is still
    alive: the reader may append later, but a returned result is always a consistent prefix.
    """

    limit: int
    chunks: list[bytes] = field(default_factory=list)
    byte_count: int = 0
    truncated: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def append(self, chunk: bytes) -> None:
        with self._lock:
            room = self.limit - self.byte_count
            if room <= 0:
                self.truncated = True
                return
            if len(chunk) > room:
                self.chunks.append(chunk[:room])
                self.byte_count = self.limit
                self.truncated = True
                return
            self.chunks.append(chunk)
            self.byte_count += len(chunk)

    def snapshot(self) -> tuple[str, bool]:
        with self._lock:
            text = b"".join(self.chunks).decode("utf-8", errors="replace").replace("\r\n", "\n")
            return text, self.truncated

    def text(self) -> str:
        return self.snapshot()[0]


def _write_stdin(process: subprocess.Popen[Any], payload: bytes | None) -> None:
    if payload is None or process.stdin is None:
        return
    try:
        process.stdin.write(payload)
        process.stdin.flush()
    except BrokenPipeError, OSError, ValueError:
        pass
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError, OSError, ValueError:
            pass


def _read_stream(
    pipe: Any,
    capture: _StreamCapture,
    stream_name: ProcessStream,
    errors: list[BaseException],
    on_output: Callable[[ProcessStream, bytes], None] | None,
) -> None:
    try:
        while True:
            reader = getattr(pipe, "read1", None)
            chunk = reader(_READ_CHUNK_BYTES) if callable(reader) else pipe.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            capture.append(chunk)
            if on_output is not None:
                try:
                    on_output(stream_name, chunk)
                except Exception:
                    # Streaming is observational: a broken sink never alters the command result.
                    pass
    except (OSError, ValueError) as exc:
        errors.append(exc)
    finally:
        try:
            pipe.close()
        except OSError, ValueError:
            pass
