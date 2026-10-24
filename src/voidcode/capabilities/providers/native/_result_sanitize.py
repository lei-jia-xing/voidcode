"""Bounded model-visible projection of tool payloads.

Projects tool results onto the provider wire, summarizing oversized strings
and omitting inline blobs.
"""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.sdk.limits import MODEL_FIELD_CHARS as MAX_MODEL_FIELD_CHARS

_INLINE_BLOB_KEYS = frozenset({"data_uri", "dataUri", "base64", "blob"})


def _string_summary(value: str, *, include_preview: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "omitted": True,
        "byte_count": len(value.encode("utf-8")),
        "line_count": len(value.splitlines()),
    }
    if include_preview:
        preview = value[:MAX_MODEL_FIELD_CHARS]
        payload["preview"] = preview
        payload["omitted_chars"] = max(0, len(value) - len(preview))
    return payload


def _sanitize_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Return a bounded model-visible copy of one mapping."""
    return {str(item_key): _sanitize_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}


def _sanitize_value(value: object, *, key: str | None = None) -> object:
    if isinstance(value, str):
        if key in _INLINE_BLOB_KEYS:
            return _string_summary(value, include_preview=False)
        if len(value) > MAX_MODEL_FIELD_CHARS:
            return _string_summary(value, include_preview=True)
        return value
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, list | tuple):
        return [_sanitize_value(item, key=key) for item in value]
    return value


def sanitize_tool_result_data(data: Mapping[str, object]) -> dict[str, object]:
    return _sanitize_mapping(data)
