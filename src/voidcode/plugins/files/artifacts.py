"""Session artifact boundary for workspace tools.

The core owns the content-addressed artifact store (``PluginServices.spill_artifact``
/ ``read_artifact``); these helpers keep one plugin-local projection of that
store so tool call sites never speak the service protocol directly. A plugin
handler without the service keeps its inline output and reports truncation
honestly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

MAX_ARTIFACT_PAGE_BYTES = 50 * 1024 - 4096
MAX_INLINE_ATTACHMENT_BYTES = (50 * 1024 - 128) * 3 // 4
MAX_BINARY_READ_BYTES = 50 * 1024 * 1024
RASTER_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
#: The store addresses artifacts by the SHA-256 of their bytes.
ARTIFACT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def create_artifact(
    services: object,
    *,
    kind: str,
    content: str | bytes,
    mime: str,
    source: str | None = None,
) -> Mapping[str, object] | None:
    """Store bytes in the core artifact store and project their reference.

    Returns ``None`` when the handler has no artifact service; callers then keep
    bounded inline output and report truncation instead of a reference.
    """
    spill = getattr(services, "spill_artifact", None)
    if spill is None:
        return None
    data = content.encode("utf-8") if isinstance(content, str) else content
    reference = spill(data, media_type=mime)
    if reference is None:
        return None
    sha256 = str(getattr(reference, "sha256", ""))
    if ARTIFACT_ID_PATTERN.fullmatch(sha256) is None:
        return None
    payload: dict[str, object] = {
        "artifact_id": sha256,
        "sha256": sha256,
        "byte_count": int(getattr(reference, "bytes", len(data))),
        "content_type": str(getattr(reference, "media_type", mime)),
        "mime": str(getattr(reference, "media_type", mime)),
        "uri": f"voidcode://artifact/{sha256}",
        "kind": kind,
        "status": "available",
    }
    if source is not None:
        payload["source"] = source
    return payload


def read_artifact_page(
    services: object,
    artifact_id: str,
    *,
    byte_offset: int = 0,
    byte_limit: int,
) -> Mapping[str, object] | None:
    """Read one bounded byte page of an artifact; ``None`` when unavailable."""
    reader = getattr(services, "read_artifact", None)
    if reader is None or ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
        return None
    try:
        page = reader(artifact_id, offset=byte_offset, limit=byte_limit)
    except OSError, ValueError, LookupError:
        return None
    if page is None:
        return None
    data = getattr(page, "data", None)
    if not isinstance(data, (bytes, bytearray)):
        return None
    return {
        "sha256": str(getattr(page, "sha256", artifact_id)),
        "offset": int(getattr(page, "offset", byte_offset)),
        "total_bytes": int(getattr(page, "total_bytes", byte_offset + len(data))),
        "data": bytes(data),
        "eof": bool(getattr(page, "eof", False)),
        "media_type": str(getattr(page, "media_type", "") or ""),
    }


def artifact_uri(artifact: Mapping[str, object] | None) -> str | None:
    artifact_id = None if artifact is None else artifact.get("artifact_id")
    return f"voidcode://artifact/{artifact_id}" if isinstance(artifact_id, str) else None
