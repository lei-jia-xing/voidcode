"""Opt-in, bounded provider request/response traces for debugging."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

_MAX_TEXT = 32_000


def _bound(value: object) -> object:
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…[truncated]"
    if isinstance(value, list):
        return [_bound(item) for item in value[:200]]
    if isinstance(value, dict):
        return {str(key): _bound(item) for key, item in list(value.items())[:200] if key not in {"api_key", "Authorization", "authorization"}}
    return value


def write_provider_trace(*, request: dict[str, object], response: object, metadata: dict[str, object]) -> None:
    """Append one redacted trace when VOIDCODE_PROVIDER_TRACE is enabled."""
    if os.environ.get("VOIDCODE_PROVIDER_TRACE", "").lower() not in {"1", "true", "yes", "on"}:
        return
    path = Path(os.environ.get("VOIDCODE_PROVIDER_TRACE_LOG", "/tmp/voidcode-provider-trace.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "trace_id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "metadata": _bound(metadata),
        "request": _bound(request),
        "response": _bound(response),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
