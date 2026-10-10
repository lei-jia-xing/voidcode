from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO

from .output import create_tool_output_artifact

_MAX_CAPTURE_BYTES = 128 * 1024 * 1024
_PREVIEW_BYTES = 25 * 1024
_CHUNK_BYTES = 8192


@dataclass(slots=True)
class ProcessOutputCapture:
    """Bounded full process capture with an independently bounded preview."""

    stream_name: str
    file: BinaryIO
    byte_count: int = 0
    limit_exceeded: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def create(cls, stream_name: str) -> ProcessOutputCapture:
        return cls(stream_name, tempfile.TemporaryFile(mode="w+b"))

    def append(self, chunk: bytes) -> None:
        with self._lock:
            room = max(0, _MAX_CAPTURE_BYTES - self.byte_count)
            retained = chunk[:room]
            if retained:
                self.file.write(retained)
                self.byte_count += len(retained)
            if len(chunk) > room:
                self.limit_exceeded = True

    def preview(self) -> str:
        with self._lock:
            self.file.flush()
            self.file.seek(0)
            if self.byte_count <= _PREVIEW_BYTES * 2:
                data = self.file.read()
            else:
                start = self.file.read(_PREVIEW_BYTES)
                self.file.seek(-_PREVIEW_BYTES, 2)
                data = start + b"\n... output preview omitted ...\n" + self.file.read()
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n")

    @property
    def preview_truncated(self) -> bool:
        return self.byte_count > _PREVIEW_BYTES * 2

    def artifact(self, *, session_id: str | None, tool_call_id: str | None, tool_name: str) -> dict[str, object] | None:
        if self.byte_count == 0:
            return None
        with self._lock:
            self.file.flush()
            self.file.seek(0)
            return create_tool_output_artifact(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                stream=self.file,
                kind=self.stream_name,
                mime="text/plain; charset=utf-8",
                source="process_output",
            )

    def close(self) -> None:
        self.file.close()


def artifact_uri(artifact: dict[str, object] | None) -> str | None:
    artifact_id = artifact.get("artifact_id") if artifact is not None else None
    return f"voidcode://artifact/{artifact_id}" if isinstance(artifact_id, str) else None


def collect_process_pipe(
    pipe: BinaryIO | None,
    capture: ProcessOutputCapture,
    *,
    on_chunk: Callable[[bytes], None] | None = None,
) -> None:
    if pipe is None:
        return
    try:
        while chunk := pipe.read(_CHUNK_BYTES):
            capture.append(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
    finally:
        pipe.close()
