"""Opt-in provider traces with a bounded record and private file permissions."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

_MAX_BYTES = 32_000
_LOG = logging.getLogger(__name__)


def _bounded_record(record: dict[str, object]) -> dict[str, object]:
    chunks: list[bytes] = []
    remaining = _MAX_BYTES
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True).iterencode(record):
        encoded = chunk.encode("utf-8")
        if len(encoded) > remaining:
            chunks.append(encoded[:remaining])
            return {
                "trace_id": record["trace_id"],
                "timestamp": record["timestamp"],
                "truncated": True,
                "preview": b"".join(chunks).decode("utf-8", errors="ignore"),
            }
        chunks.append(encoded)
        remaining -= len(encoded)
    return record


def write_provider_trace(*, request: dict[str, object], response: object, metadata: dict[str, object]) -> None:
    """Trace failures are visible in logs without failing a completed model call."""
    if os.environ.get("VOIDCODE_PROVIDER_TRACE", "").lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        record = _bounded_record(
            {
                "trace_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "metadata": metadata,
                "request": request,
                "response": response,
            }
        )
        path = Path(os.environ.get("VOIDCODE_PROVIDER_TRACE_LOG", "/tmp/voidcode-provider-trace.jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n")
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        # Exception messages may contain request data; report only their class.
        _LOG.warning("Provider trace could not be written (%s)", type(exc).__name__)
