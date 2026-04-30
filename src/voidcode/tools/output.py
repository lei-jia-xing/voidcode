from __future__ import annotations

import hashlib
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

from .contracts import ToolResult

MAX_TOOL_OUTPUT_LINES = 2000
MAX_TOOL_OUTPUT_BYTES = 50 * 1024
MAX_MODEL_FIELD_CHARS = 4000
_SENSITIVE_TEXT_ARGUMENT_KEYS = frozenset(
    {
        "content",
        "newString",
        "oldString",
        "patch",
        "edits",
        "todos",
    }
)
_INLINE_BLOB_KEYS = frozenset({"data_uri", "dataUri", "base64", "blob"})
_ARTIFACT_REFERENCE_PREFIX = "artifact:"


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


def _sanitize_value(value: object, *, key: str | None = None, argument: bool = False) -> object:
    if isinstance(value, str):
        if key in _INLINE_BLOB_KEYS:
            return _string_summary(value, include_preview=False)
        if key in _SENSITIVE_TEXT_ARGUMENT_KEYS:
            return _string_summary(value, include_preview=False)
        if len(value) > MAX_MODEL_FIELD_CHARS:
            return _string_summary(value, include_preview=True)
        return value
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_value(
                item_value,
                key=str(item_key),
                argument=argument,
            )
            for item_key, item_value in cast(dict[object, object], value).items()
        }
    if isinstance(value, list):
        return [
            _sanitize_value(item, key=key, argument=argument) for item in cast(list[object], value)
        ]
    if isinstance(value, tuple):
        return [
            _sanitize_value(item, key=key, argument=argument)
            for item in cast(tuple[object, ...], value)
        ]
    return value


def sanitize_tool_arguments(arguments: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], _sanitize_value(arguments, argument=True))


def sanitize_tool_data(data: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], _sanitize_value(data, argument=False))


def sanitize_tool_result_data(data: dict[str, object]) -> dict[str, object]:
    sanitized = sanitize_tool_data(data)
    raw_arguments = data.get("arguments")
    if isinstance(raw_arguments, dict):
        sanitized["arguments"] = sanitize_tool_arguments(cast(dict[str, object], raw_arguments))
    return sanitized


def _utf8_prefix(text: str, *, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _preview_text(content: str, *, max_lines: int, max_bytes: int) -> str:
    lines = content.splitlines(keepends=True)
    line_limited = "".join(lines[:max_lines]) if len(lines) > max_lines else content
    return _utf8_prefix(line_limited, max_bytes=max_bytes)


def _safe_artifact_segment(value: str | None, *, fallback: str) -> str:
    raw = value if value else fallback
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw)
    return safe[:96] or fallback


def _artifact_id(
    *,
    session_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    content_hash: str,
) -> str:
    digest = hashlib.sha256(
        f"{session_id or ''}\0{tool_call_id or ''}\0{tool_name}\0{content_hash}".encode()
    ).hexdigest()[:24]
    return f"artifact_{digest}"


def _tool_output_artifact_path(
    *,
    session_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    artifact_id: str,
) -> Path:
    temp_root = Path(tempfile.gettempdir()) / "voidcode-tool-output"
    session_segment = _safe_artifact_segment(session_id, fallback="unknown-session")
    call_segment = _safe_artifact_segment(tool_call_id, fallback="unknown-tool-call")
    tool_segment = _safe_artifact_segment(tool_name, fallback="tool")
    return temp_root / session_segment / f"{call_segment}-{tool_segment}-{artifact_id}.txt"


def _artifact_metadata(
    *,
    session_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    content: str,
    kind: str,
) -> dict[str, object]:
    encoded = content.encode("utf-8")
    content_hash = hashlib.sha256(encoded).hexdigest()
    artifact_id = _artifact_id(
        session_id=session_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        content_hash=content_hash,
    )
    artifact_path = _tool_output_artifact_path(
        session_id=session_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        artifact_id=artifact_id,
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(content, encoding="utf-8", newline="\n")
    return {
        "artifact_id": artifact_id,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "kind": kind,
        "byte_count": len(encoded),
        "line_count": len(content.splitlines()),
        "sha256": content_hash,
        "content_type": "text/plain; charset=utf-8",
        "created_at": int(time.time() * 1000),
        "path": str(artifact_path),
        "uri": artifact_path.resolve().as_uri(),
        "status": "available",
    }


def _artifact_missing_payload(artifact: Mapping[str, object]) -> dict[str, object]:
    return {
        "artifact_id": artifact.get("artifact_id"),
        "status": "missing",
        "artifact_missing": True,
        "path": artifact.get("path"),
    }


def _artifact_path_from_metadata(artifact: Mapping[str, object]) -> Path | None:
    raw_path = artifact.get("path")
    if not isinstance(raw_path, str) or raw_path == "":
        return None
    return Path(raw_path)


def read_tool_output_artifact(
    artifact: Mapping[str, object],
    *,
    offset: int = 0,
    limit: int = 2000,
) -> dict[str, object]:
    """Read a bounded line slice from a temp-backed tool output artifact."""

    path = _artifact_path_from_metadata(artifact)
    if path is None or not path.is_file():
        return _artifact_missing_payload(artifact)

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = max(0, offset)
    bounded_limit = max(0, limit)
    selected = lines[start : start + bounded_limit]
    return {
        "artifact_id": artifact.get("artifact_id"),
        "status": "available",
        "artifact_missing": False,
        "offset": start,
        "limit": bounded_limit,
        "line_count": len(lines),
        "next_offset": start + len(selected) if start + len(selected) < len(lines) else None,
        "content": "".join(selected),
    }


def search_tool_output_artifact(
    artifact: Mapping[str, object],
    *,
    pattern: str,
    case_sensitive: bool = False,
    limit: int = 100,
) -> dict[str, object]:
    """Search a temp-backed tool output artifact and return bounded matching lines."""

    path = _artifact_path_from_metadata(artifact)
    if path is None or not path.is_file():
        return _artifact_missing_payload(artifact)

    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)
    matches: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if regex.search(line):
            matches.append({"line_number": line_number, "line": line})
            if len(matches) >= max(0, limit):
                break
    return {
        "artifact_id": artifact.get("artifact_id"),
        "status": "available",
        "artifact_missing": False,
        "pattern": pattern,
        "match_count": len(matches),
        "matches": matches,
    }


def cap_tool_result_output(
    result: ToolResult,
    *,
    workspace: Path,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    max_lines: int = MAX_TOOL_OUTPUT_LINES,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> ToolResult:
    """Cap model-visible tool output/error text and save the full text as a temp artifact."""

    _ = workspace  # kept for compatibility with existing tool-output capping call sites

    if result.content is None or result.content == "":
        if result.error is None or result.error == "":
            return result
        error_size = len(result.error.encode("utf-8"))
        error_lines = len(result.error.splitlines())
        if error_size <= max_bytes and error_lines <= max_lines:
            return result
        artifact = _artifact_metadata(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=result.tool_name,
            content=result.error,
            kind="error",
        )
        preview = _preview_text(result.error, max_lines=max_lines, max_bytes=max_bytes)
        reference = f"{_ARTIFACT_REFERENCE_PREFIX}{artifact['artifact_id']}"
        hint = (
            "\n\n[Tool error truncated: "
            f"artifact_id={artifact['artifact_id']}. "
            "Use artifact retrieval by artifact_id or tool_call_id to read/search the full error.]"
        )
        return replace(
            result,
            error=f"{preview}{hint}",
            data={
                **result.data,
                "truncated": True,
                "artifact": artifact,
                "artifact_id": artifact["artifact_id"],
                "artifact_status": "available",
                "artifact_missing": False,
                "output_path": artifact["path"],
                "original_error_byte_count": error_size,
                "original_error_line_count": error_lines,
                "tool_output_max_bytes": max_bytes,
                "tool_output_max_lines": max_lines,
            },
            truncated=True,
            partial=True,
            reference=reference,
        )

    content = result.content
    encoded_size = len(content.encode("utf-8"))
    line_count = len(content.splitlines())
    if encoded_size <= max_bytes and line_count <= max_lines:
        return result

    artifact = _artifact_metadata(
        session_id=session_id,
        tool_call_id=tool_call_id,
        tool_name=result.tool_name,
        content=content,
        kind="content",
    )

    preview = _preview_text(content, max_lines=max_lines, max_bytes=max_bytes)
    omitted_bytes = max(0, encoded_size - len(preview.encode("utf-8")))
    omitted_lines = max(0, line_count - len(preview.splitlines()))
    reference = f"{_ARTIFACT_REFERENCE_PREFIX}{artifact['artifact_id']}"
    hint = (
        "\n\n[Tool output truncated: "
        f"omitted {omitted_bytes} bytes and {omitted_lines} lines. "
        f"artifact_id={artifact['artifact_id']}. "
        "Use artifact retrieval by artifact_id or tool_call_id to read/search the full output.]"
    )

    return replace(
        result,
        content=f"{preview}{hint}",
        data={
            **result.data,
            "truncated": True,
            "artifact": artifact,
            "artifact_id": artifact["artifact_id"],
            "artifact_status": "available",
            "artifact_missing": False,
            "output_path": artifact["path"],
            "original_byte_count": encoded_size,
            "original_line_count": line_count,
            "tool_output_max_bytes": max_bytes,
            "tool_output_max_lines": max_lines,
        },
        truncated=True,
        partial=True,
        reference=reference,
    )


__all__ = [
    "MAX_MODEL_FIELD_CHARS",
    "MAX_TOOL_OUTPUT_BYTES",
    "MAX_TOOL_OUTPUT_LINES",
    "cap_tool_result_output",
    "read_tool_output_artifact",
    "sanitize_tool_arguments",
    "sanitize_tool_data",
    "sanitize_tool_result_data",
    "search_tool_output_artifact",
]
