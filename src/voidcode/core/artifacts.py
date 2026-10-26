"""Content-addressed artifacts the core owns under its state directory.

An artifact's path is its own digest, so identity and integrity are the same fact, a repeated spill
is free, and nothing is ever rewritten. Spill-then-commit is the contract that matters: the bytes
land at their final path before returning, so any durable result that references an artifact
commits strictly after the artifact exists. Nothing is garbage collected, and a spilled blob can
never be confused with an admitted plugin artifact (that is a directory, this is a file).
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final

#: Content address: a sha256 digest, and nothing else — never a caller-supplied path.
SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
#: Bounded chunk size for streaming artifact spills.
CHUNK_BYTES: Final[int] = 256 * 1024
#: One artifact is bounded so a runaway producer cannot exhaust the disk through this service.
MAX_ARTIFACT_BYTES: Final[int] = 256 * 1024 * 1024
#: Bounded page size for reading artifact chunks.
MAX_PAGE_BYTES: Final[int] = 256 * 1024

_MODE: Final[int] = 0o600
#: A media type is a bounded, printable claim; a claim that fails the bound is refused with
#: :class:`ArtifactError` rather than stored as a guess.
_MEDIA_TYPE_MAX: Final[int] = 128


class ArtifactError(ValueError):
    """A spill or read that the store refuses, always with a reason a caller can act on."""


def _valid_media_type(value: str) -> bool:
    return bool(value) and len(value) <= _MEDIA_TYPE_MAX and all(char.isprintable() and char != "\x7f" for char in value)


def normalize_media_type(value: object) -> str:
    """Validate a producer's media-type claim; a refused claim is a visible error, not a default."""
    text = "" if value is None else str(value)
    if not _valid_media_type(text):
        raise ArtifactError("artifact media_type must be a short printable string")
    return text


@dataclass(slots=True)
class Spill:
    """One in-progress streaming write; private to the store until it is finalized."""

    owner: str
    path: Path
    media_type: str
    handle: BinaryIO
    digest: Any = field(default_factory=hashlib.sha256)
    written: int = 0

    def append(self, data: bytes) -> None:
        if self.written + len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        self.handle.write(data)
        self.digest.update(data)
        self.written += len(data)

    def close(self) -> None:
        try:
            self.handle.close()
        except OSError:
            pass


class ArtifactStore:
    """The workspace's artifact directory, used only through content addresses."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._spills: dict[str, Spill] = {}

    def start_spill(self, media_type: str, *, owner: str) -> tuple[str, Spill]:
        self.root.mkdir(parents=True, exist_ok=True)
        spill_id = uuid.uuid4().hex
        # The temporary name is dot-prefixed and is not a digest, so it can never be read back
        # as an artifact, even while the write is still in flight.
        path = self.root / f".spill-{spill_id}"
        spill = Spill(owner=owner, path=path, media_type=media_type, handle=path.open("xb"))
        os.chmod(path, _MODE)
        self._spills[spill_id] = spill
        return spill_id, spill

    def spill(self, spill_id: str) -> Spill:
        spill = self._spills.get(spill_id)
        if spill is None:
            raise ArtifactError(f"unknown spill: {spill_id!r}")
        return spill

    def finish(self, spill_id: str) -> tuple[str, int, str]:
        """Publish the spill under its digest and return `(sha256, bytes, media_type)`."""
        spill = self.spill(spill_id)
        self._spills.pop(spill_id, None)
        try:
            spill.handle.flush()
            os.fsync(spill.handle.fileno())
        except OSError:
            pass
        spill.close()
        digest = spill.digest.hexdigest()
        destination = self.root / digest
        try:
            # A hard link publishes atomically and refuses an existing path, so a stored artifact
            # is never rewritten and a collision is visible rather than silent.
            os.link(spill.path, destination)
        except FileExistsError:
            # Identity is the digest: the same bytes are already stored, so this is a no-op.
            pass
        finally:
            spill.path.unlink(missing_ok=True)
        if destination.stat().st_size != spill.written:
            raise ArtifactError(f"stored artifact {digest} does not match the spilled bytes")
        self._write_media_type(digest, spill.media_type)
        self._sync_directory()
        return digest, spill.written, spill.media_type

    def _write_media_type(self, digest: str, media_type: str) -> None:
        """Record the producer's type claim once; the bytes stay addressed by content alone."""
        if not media_type:
            return
        sidecar = self.root / f"{digest}.media"
        try:
            with sidecar.open("x", encoding="utf-8") as handle:
                handle.write(media_type)
            os.chmod(sidecar, _MODE)
        except FileExistsError:
            # Another producer already stored these bytes with a type: the first claim stands.
            pass

    def _sync_directory(self) -> None:
        """Make the new name durable before any fact that references it may commit."""
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def discard(self, spill_id: str) -> None:
        spill = self._spills.pop(spill_id, None)
        if spill is not None:
            spill.close()
            spill.path.unlink(missing_ok=True)

    def discard_all(self) -> None:
        for spill_id in tuple(self._spills):
            self.discard(spill_id)

    def discard_owner(self, token: str) -> None:
        """Drop the unfinished spills of one lease; a lease can never outlive its own bytes."""
        for spill_id, spill in tuple(self._spills.items()):
            if spill.owner == token:
                self.discard(spill_id)

    def resolve(self, sha256: str) -> Path:
        """The path of one stored artifact, refused for anything that is not a digest."""
        if not isinstance(sha256, str) or not SHA256_PATTERN.match(sha256):
            raise ArtifactError("artifact reference must be a lowercase sha256 digest")
        path = self.root / sha256
        if not path.is_file():
            raise ArtifactError(f"artifact is not stored: {sha256}")
        return path

    def media_type(self, sha256: str) -> str:
        """The media type the artifact was stored with, when its producer declared one.

        The bytes are addressed by content, so their type is a claim stored beside them: the first
        declaration wins and a later reader with no declaration of its own recovers this one.
        """
        sidecar = self.root / f"{sha256}.media"
        try:
            value = sidecar.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return value if _valid_media_type(value) else ""

    def read(self, sha256: str, *, offset: int, limit: int) -> tuple[bytes, int, bool, str]:
        """Read one bounded page; returns `(data, total_bytes, eof, media_type)`."""
        if offset < 0:
            raise ArtifactError("artifact offset must not be negative")
        if limit <= 0 or limit > MAX_PAGE_BYTES:
            raise ArtifactError(f"artifact page limit must be between 1 and {MAX_PAGE_BYTES}")
        path = self.resolve(sha256)
        total = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit)
        return data, total, offset + len(data) >= total, self.media_type(sha256)
