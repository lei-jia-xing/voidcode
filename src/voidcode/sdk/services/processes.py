"""Child-process handles, output capture, and typed execution failures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .artifacts import ArtifactReference

if TYPE_CHECKING:
    from ..observed_effects import ObservedEffects

type ProcessStream = Literal["stdout", "stderr"]

type ProcessFailureKind = Literal["start_failed", "capture_failed", "sandbox_failed", "process_failed"]


class ProcessFailure(ValueError):
    """A process invocation failed; ``kind`` is the stable classification."""

    kind: ProcessFailureKind
    detail: str

    def __init__(self, kind: ProcessFailureKind, detail: str) -> None:
        if kind not in {"start_failed", "capture_failed", "sandbox_failed", "process_failed"}:
            raise ValueError(f"unknown process failure kind: {kind!r}")
        if not isinstance(detail, str) or not detail:
            raise ValueError("process failure detail must be non-empty text")
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class ProcessAlreadyRunningError(ValueError):
    """A spawn reuses a process id held by a live process.

    Callers branch on this type, never on the message, so wording is free.
    """

    def __init__(self, process_id: str) -> None:
        super().__init__(f"background process {process_id} is already running")
        self.process_id = process_id


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    """Identity of one supervised background process, as the caller persists it.

    `pid` alone is not an identity: `start_time` is the core's own start stamp for that pid, so a
    later reuse of the number can never be mistaken for this process.
    """

    process_id: str
    pid: int
    start_time: str
    stdout_path: str
    stderr_path: str
    running: bool = True
    exit_code: int | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "pid": self.pid,
            "start_time": self.start_time,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "running": self.running,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """One bounded page of a supervised process's log, with its liveness at read time."""

    process_id: str
    stream: ProcessStream
    offset: int
    total_bytes: int
    data: bytes
    eof: bool
    running: bool

    @property
    def text(self) -> str:
        """The page decoded UTF-8 (invalid bytes replaced) for callers that page text."""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class RawStreamCapture:
    """One raw byte stream durably captured to its own artifact.

    Exactly one of ``artifact`` (stored bytes) and ``capture_error`` (the
    verbatim store/append/finish cause) is set, so a silent partial is
    unrepresentable. ``truncated`` is ``bytes_dropped > 0`` regardless of
    cause: bound truncation and post-store-failure drops both count.
    """

    stream: ProcessStream
    bytes_captured: int
    bytes_dropped: int
    artifact: ArtifactReference | None
    capture_error: str | None = None

    def __post_init__(self) -> None:
        if self.stream not in ("stdout", "stderr"):
            raise ValueError(f"raw stream capture requires 'stdout' or 'stderr', got {self.stream!r}")
        for name in ("bytes_captured", "bytes_dropped"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"raw stream capture {name} must be a non-negative integer")
        if (self.artifact is None) == (self.capture_error is None):
            raise ValueError("raw stream capture requires exactly one of artifact or capture_error")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactReference):
            raise ValueError("raw stream capture artifact must be an ArtifactReference or None")
        if self.capture_error is not None and (not isinstance(self.capture_error, str) or not self.capture_error):
            raise ValueError("raw stream capture capture_error must be non-empty text or None")

    @property
    def truncated(self) -> bool:
        return self.bytes_dropped > 0

    def as_payload(self) -> dict[str, object]:
        return {
            "stream": self.stream,
            "bytes_captured": self.bytes_captured,
            "bytes_dropped": self.bytes_dropped,
            "truncated": self.truncated,
            "artifact": None if self.artifact is None else self.artifact.as_payload(),
            "capture_error": self.capture_error,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> RawStreamCapture:
        if not isinstance(payload, Mapping):
            raise ValueError("raw stream capture payload must be an object")
        allowed = {"stream", "bytes_captured", "bytes_dropped", "truncated", "artifact", "capture_error"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("raw stream capture contains unknown field(s): " + ", ".join(unknown))
        for name in ("stream", "bytes_captured", "bytes_dropped", "artifact", "capture_error"):
            if name not in payload:
                raise ValueError(f"raw stream capture is missing {name}")
        raw_stream = payload["stream"]
        if raw_stream == "stdout":
            stream: ProcessStream = "stdout"
        elif raw_stream == "stderr":
            stream = "stderr"
        else:
            raise ValueError(f"raw stream capture requires 'stdout' or 'stderr', got {raw_stream!r}")
        captured = payload["bytes_captured"]
        dropped = payload["bytes_dropped"]
        if type(captured) is not int or captured < 0:
            raise ValueError("raw stream capture bytes_captured must be a non-negative integer")
        if type(dropped) is not int or dropped < 0:
            raise ValueError("raw stream capture bytes_dropped must be a non-negative integer")
        error = payload["capture_error"]
        if error is not None and (not isinstance(error, str) or not error):
            raise ValueError("raw stream capture capture_error must be non-empty text or None")
        if "truncated" in payload:
            truncated = payload["truncated"]
            if type(truncated) is not bool or truncated != (dropped > 0):
                raise ValueError("raw stream capture truncated flag does not match bytes_dropped")
        raw_artifact = payload["artifact"]
        reference: ArtifactReference | None
        if raw_artifact is None:
            reference = None
        else:
            if not isinstance(raw_artifact, Mapping) or set(raw_artifact) != {"sha256", "bytes", "media_type"}:
                raise ValueError("raw stream capture artifact must be an artifact reference or null")
            sha256 = raw_artifact["sha256"]
            total = raw_artifact["bytes"]
            media_type = raw_artifact["media_type"]
            if not isinstance(sha256, str) or type(total) is not int or not isinstance(media_type, str):
                raise ValueError("raw stream capture artifact fields have the wrong types")
            reference = ArtifactReference(sha256=sha256, bytes=total, media_type=media_type)
        return RawStreamCapture(
            stream=stream,
            bytes_captured=captured,
            bytes_dropped=dropped,
            artifact=reference,
            capture_error=error,
        )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of one lease-scoped child process execution."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    #: Bounded observed-effects report when this execution ran confined; None on the
    #: unconfined/absent path. Failed/cancelled/timed-out runs still carry the prefix.
    effects: ObservedEffects | None = None
    #: Raw byte captures: empty in text mode (today); exactly (stdout, stderr) when requested.
    raw: tuple[RawStreamCapture, ...] = ()
    #: Why capture is a partial prefix. ``reader_drain_timeout`` means the child exited but a
    #: pipe reader had not reached EOF before the bounded post-exit drain deadline; stdout and
    #: stderr then hold only the bytes read so far and ``truncated`` is true.
    capture_issue: Literal["reader_drain_timeout"] | None = None

    def __post_init__(self) -> None:
        raw = tuple(self.raw)
        object.__setattr__(self, "raw", raw)
        if any(not isinstance(entry, RawStreamCapture) for entry in raw):
            raise ValueError("process result raw captures must be RawStreamCapture entries")
        if len(raw) not in (0, 2):
            raise ValueError("process result raw must be empty or exactly its stdout/stderr pair")
        if len(raw) == 2 and (raw[0].stream != "stdout" or raw[1].stream != "stderr"):
            raise ValueError("process result raw captures must arrive in stdout/stderr order")
        if self.capture_issue not in (None, "reader_drain_timeout"):
            raise ValueError(f"unknown process capture issue: {self.capture_issue!r}")
        if self.capture_issue is not None and not self.truncated:
            raise ValueError("a process capture issue requires truncated=True")
