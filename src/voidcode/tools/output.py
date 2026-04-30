from __future__ import annotations

import hashlib
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


def _is_metadata_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_redaction_summary(value: dict[object, object]) -> bool:
    return (
        value.get("omitted") is True
        and _is_metadata_count(value.get("byte_count"))
        and _is_metadata_count(value.get("line_count"))
        and "preview" not in value
        and "omitted_chars" not in value
    )


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


def strip_redaction_sentinels(value: object) -> object:
    """Return a schema-safe copy with redaction metadata placeholders removed."""

    if isinstance(value, dict):
        raw_value = cast(dict[object, object], value)
        if _is_redaction_summary(raw_value):
            return ""
        return {str(key): strip_redaction_sentinels(item) for key, item in raw_value.items()}
    if isinstance(value, list):
        return [strip_redaction_sentinels(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return [strip_redaction_sentinels(item) for item in cast(tuple[object, ...], value)]
    return value


def _utf8_prefix(text: str, *, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _preview_text(content: str, *, max_lines: int, max_bytes: int) -> str:
    lines = content.splitlines(keepends=True)
    line_limited = "".join(lines[:max_lines]) if len(lines) > max_lines else content
    return _utf8_prefix(line_limited, max_bytes=max_bytes)


def _tool_output_reference_path(workspace: Path, tool_name: str, content: str) -> Path:
    digest = hashlib.sha256(f"{tool_name}\0{content}".encode()).hexdigest()[:24]
    safe_tool_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in tool_name)
    return workspace / ".voidcode" / "tool-output" / f"{safe_tool_name}-{digest}.txt"


def cap_tool_result_output(
    result: ToolResult,
    *,
    workspace: Path,
    max_lines: int = MAX_TOOL_OUTPUT_LINES,
    max_bytes: int = MAX_TOOL_OUTPUT_BYTES,
) -> ToolResult:
    """Cap model-visible tool output/error text and save the full text by reference."""

    if result.content is None or result.content == "":
        if result.error is None or result.error == "":
            return result
        error_size = len(result.error.encode("utf-8"))
        error_lines = len(result.error.splitlines())
        if error_size <= max_bytes and error_lines <= max_lines:
            return result
        output_path = _tool_output_reference_path(
            workspace.resolve(), result.tool_name, result.error
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.error, encoding="utf-8", newline="\n")
        preview = _preview_text(result.error, max_lines=max_lines, max_bytes=max_bytes)
        reference = output_path.relative_to(workspace.resolve()).as_posix()
        hint = f"\n\n[Tool error truncated: Full error saved to: {reference}]"
        return replace(
            result,
            error=f"{preview}{hint}",
            data={
                **result.data,
                "truncated": True,
                "output_path": reference,
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

    output_path = _tool_output_reference_path(workspace.resolve(), result.tool_name, content)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8", newline="\n")

    preview = _preview_text(content, max_lines=max_lines, max_bytes=max_bytes)
    omitted_bytes = max(0, encoded_size - len(preview.encode("utf-8")))
    omitted_lines = max(0, line_count - len(preview.splitlines()))
    reference = output_path.relative_to(workspace.resolve()).as_posix()
    hint = (
        "\n\n[Tool output truncated: "
        f"omitted {omitted_bytes} bytes and {omitted_lines} lines. "
        f"Full output saved to: {reference}]"
    )

    return replace(
        result,
        content=f"{preview}{hint}",
        data={
            **result.data,
            "truncated": True,
            "output_path": reference,
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
    "sanitize_tool_arguments",
    "sanitize_tool_data",
    "sanitize_tool_result_data",
    "strip_redaction_sentinels",
]
