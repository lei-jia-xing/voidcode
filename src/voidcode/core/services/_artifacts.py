"""Artifact services: immutable spills and bounded reads."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import CancelledError

from voidcode.sdk.services import ArtifactPage, ArtifactReference

from ..artifacts import SHA256_PATTERN, ArtifactNotFoundError, normalize_media_type
from ._lease import ServiceLease


class ArtifactServices(ServiceLease):
    """Immutable artifact spills and bounded reads."""

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        if not isinstance(data, (bytes, str)):
            raise TypeError("artifact data must be bytes or text")
        payload = data.encode("utf-8") if isinstance(data, str) else data
        return self.spill_artifact_stream((payload,), media_type=media_type)

    def spill_artifact_stream(self, chunks: Iterable[bytes], *, media_type: str = "application/octet-stream") -> ArtifactReference:
        lease = self._active_lease()
        if lease.identity.hook:
            raise PermissionError("hook leases cannot spill artifacts: hooks observe, they produce no result")
        store = self._runtime.require_artifacts()
        spill_id, spill = store.start_spill(normalize_media_type(media_type), owner=lease.identity.token)
        try:
            for chunk in chunks:
                self._active_lease()
                if self._runtime.cancel_event_for(lease).is_set() or (lease.abort_signal is not None and lease.abort_signal.cancelled):
                    raise CancelledError("artifact spill cancelled")
                if not isinstance(chunk, bytes):
                    raise TypeError("artifact stream chunks must be bytes")
                spill.append(chunk)
            self._active_lease()
            if self._runtime.cancel_event_for(lease).is_set() or (lease.abort_signal is not None and lease.abort_signal.cancelled):
                raise CancelledError("artifact spill cancelled")
            digest, size, resolved = store.finish(spill_id)
            self._active_lease()
        except BaseException:
            store.discard(spill_id)
            raise
        return ArtifactReference(sha256=digest, bytes=size, media_type=resolved)

    def read_artifact(
        self,
        sha256: str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        self._active_lease()
        if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
            raise ValueError("artifact reference must be a lowercase sha256 digest")
        try:
            data, total, eof, media_type = self._runtime.require_artifacts().read(sha256, offset=offset, limit=limit)
        except ArtifactNotFoundError as exc:
            raise FileNotFoundError(f"artifact is not stored: {sha256}") from exc
        return ArtifactPage(
            artifact=ArtifactReference(sha256, total, media_type),
            offset=offset,
            data=data,
            eof=eof,
        )
