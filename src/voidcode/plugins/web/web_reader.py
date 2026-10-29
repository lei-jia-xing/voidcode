"""The ``web://`` namespace: already-fetched rendered page snapshots."""

from __future__ import annotations

import re

from voidcode.capabilities.resource_pages import render_stored_artifact_page
from voidcode.sdk.resources import (
    ResourcePage,
    ResourceReadCaller,
    ResourceReader,
    ResourceReaderServices,
    ResourceReadRequest,
)

WEB_URI_PREFIX = "web://"
_WEB_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def read_web_snapshot(request: ResourceReadRequest, caller: ResourceReadCaller, services: ResourceReaderServices) -> ResourcePage | None:
    """Serve one bounded page of a fetched snapshot; ``None`` when the digest is not stored."""
    digest = request.uri.partition("://")[2].strip()
    if not digest:
        raise ValueError("web://<sha256> requires an artifact digest")
    if _WEB_DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("web:// requires a lowercase sha256 digest")
    return render_stored_artifact_page(
        uri=request.uri,
        digest=digest,
        services=services,
        caller=caller,
        offset=request.offset or 1,
        limit=request.limit or 2000,
        byte_offset=request.byte_offset,
        byte_limit=request.byte_limit,
    )


WEB_READER = ResourceReader("web", read_web_snapshot)
