from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ..security.path_policy import resolve_workspace_path

# A preview is an observation for the live stream, not a second write path. Keep
# both the workspace read and the client projection bounded independently.
PREVIEW_SNAPSHOT_MAX_BYTES = 64 * 1024
PREVIEW_DIFF_MAX_CHARS = 8 * 1024
PREVIEW_PATH_MAX_CHARS = 512
PREVIEW_MAX_PATHS = 32
PREVIEW_MAX_EDITS = 64

WRITE_PREVIEW_TOOLS = frozenset({"write", "edit", "multi_edit", "apply_patch"})

_SECRET_TEXT_PATTERN = re.compile(
    r"(?i)(?P<key>api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret|token)"
    r"(?P<sep>\s*[:=]\s*)(?P<value>[^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_PREFIX_PATTERN = re.compile(r"\b(?:sk|gh[pousr]|xox[baprs])-[A-Za-z0-9_-]{12,}\b")


@dataclass(frozen=True, slots=True)
class _PartialArguments:
    values: dict[str, object]
    incomplete: bool = True


def _utf8_prefix(value: str, *, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _bounded_path(value: str) -> str:
    redacted = _redact_text(value)
    return redacted[:PREVIEW_PATH_MAX_CHARS]


def _redact_text(value: str) -> str:
    value = _SECRET_TEXT_PATTERN.sub(r"\g<key>\g<sep><redacted>", value)
    value = _BEARER_PATTERN.sub(r"\g<1><redacted>", value)
    return _SECRET_PREFIX_PATTERN.sub("<redacted>", value)


def _bounded_diff(value: str) -> tuple[str, bool]:
    redacted = _redact_text(value)
    return _utf8_prefix(redacted, max_bytes=PREVIEW_DIFF_MAX_CHARS)


def _degraded(
    *,
    tool_name: str,
    reason: str,
    path: str | None = None,
    paths: list[str] | None = None,
    phase: Literal["partial", "final"],
) -> dict[str, object]:
    bounded_paths = [_bounded_path(item) for item in (paths or [])[:PREVIEW_MAX_PATHS] if item]
    result: dict[str, object] = {
        "schema_version": 1,
        "phase": phase,
        "live_only": phase == "partial",
        "tool": tool_name,
        "status": "degraded",
        "bounded": True,
        "truncated": False,
        "reason": reason,
    }
    if path:
        result["path"] = _bounded_path(path)
    if bounded_paths:
        result["paths"] = bounded_paths
    return result


def _resolve_preview_path(*, workspace: Path, raw_path: object) -> tuple[Path, str] | dict[str, object]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _degraded(tool_name="", reason="missing_path", phase="partial")
    if len(raw_path) > PREVIEW_PATH_MAX_CHARS:
        return _degraded(tool_name="", reason="path_too_long", path=raw_path, phase="partial")
    try:
        resolution = resolve_workspace_path(
            workspace=workspace,
            raw_path=raw_path,
            containment_error="preview path must be inside the workspace",
            allow_outside_workspace=False,
        )
    except (OSError, RuntimeError, ValueError):
        return _degraded(tool_name="", reason="unsafe_path", path=raw_path, phase="partial")
    return resolution.candidate, resolution.relative_path


def _read_snapshot(path: Path) -> tuple[str, bool] | str:
    try:
        if not path.exists():
            return "", False
        if not path.is_file():
            return "snapshot_not_regular_file"
        with path.open("rb") as handle:
            raw = handle.read(PREVIEW_SNAPSHOT_MAX_BYTES + 1)
        if len(raw) > PREVIEW_SNAPSHOT_MAX_BYTES:
            return "snapshot_too_large"
        return raw.decode("utf-8"), True
    except (OSError, UnicodeError):
        return "snapshot_unreadable"


def _diff_payload(*, path: str, before: str, after: str, phase: Literal["partial", "final"], incomplete: bool = False) -> dict[str, object]:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    bounded_diff, diff_truncated = _bounded_diff(diff)
    additions = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return {
        "schema_version": 1,
        "phase": phase,
        "live_only": phase == "partial",
        "tool": "",
        "status": "ready",
        "bounded": True,
        "path": _bounded_path(path),
        "diff": bounded_diff,
        "additions": additions,
        "deletions": deletions,
        "before_sha256": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "after_sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(),
        "truncated": diff_truncated or incomplete,
    }


def _finish_diff_payload(
    payload: dict[str, object],
    *,
    tool_name: str,
    phase: Literal["partial", "final"],
) -> dict[str, object]:
    payload["tool"] = tool_name
    payload["phase"] = phase
    payload["live_only"] = phase == "partial"
    return payload


def _extract_partial_string(text: str, key: str) -> tuple[str, bool] | None:
    # This scanner intentionally understands only JSON string fields. It never
    # exposes the raw argument fragment; an unterminated value is decoded as a
    # bounded prefix and marked incomplete for the client.
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"', text)
    if match is None:
        return None
    start = match.end()
    escaped = False
    index = start
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            encoded = '"' + text[start:index] + '"'
            try:
                value = json.loads(encoded)
            except TypeError, ValueError:
                return None
            return (value, False) if isinstance(value, str) else None
        index += 1
    body = text[start:]
    if escaped:
        body = body[:-1]
    try:
        value = json.loads('"' + body + '"')
    except TypeError, ValueError:
        return None
    return (value, True) if isinstance(value, str) else None


def _partial_arguments(argument_text: str, parsed_arguments: Mapping[str, object] | None) -> _PartialArguments:
    if parsed_arguments is not None:
        return _PartialArguments(values=dict(parsed_arguments), incomplete=False)
    try:
        parsed = json.loads(argument_text)
    except TypeError, ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return _PartialArguments(values=cast(dict[str, object], parsed), incomplete=False)

    values: dict[str, object] = {}
    incomplete = True
    for key in ("path", "content", "oldString", "newString", "patch"):
        extracted = _extract_partial_string(argument_text, key)
        if extracted is not None:
            values[key], field_incomplete = extracted
            incomplete = incomplete or field_incomplete
    return _PartialArguments(values=values, incomplete=incomplete)


def _preview_single_file(
    *,
    workspace: Path,
    tool_name: str,
    arguments: Mapping[str, object],
    phase: Literal["partial", "final"],
    incomplete: bool = False,
) -> dict[str, object]:
    resolved = _resolve_preview_path(workspace=workspace, raw_path=arguments.get("path"))
    if isinstance(resolved, dict):
        return _finish_diff_payload(resolved, tool_name=tool_name, phase=phase)
    candidate, display_path = resolved
    snapshot = _read_snapshot(candidate)
    if isinstance(snapshot, str):
        return _degraded(tool_name=tool_name, reason=snapshot, path=display_path, phase=phase)
    before, exists = snapshot

    if tool_name == "write":
        content = arguments.get("content")
        if not isinstance(content, str):
            return _degraded(tool_name=tool_name, reason="missing_content", path=display_path, phase=phase)
        try:
            content_bytes = len(content.encode("utf-8"))
        except UnicodeError:
            return _degraded(tool_name=tool_name, reason="content_unreadable", path=display_path, phase=phase)
        if content_bytes > PREVIEW_SNAPSHOT_MAX_BYTES:
            return _degraded(tool_name=tool_name, reason="proposed_content_too_large", path=display_path, phase=phase)
        return _finish_diff_payload(
            _diff_payload(path=display_path, before=before, after=content, phase=phase, incomplete=incomplete),
            tool_name=tool_name,
            phase=phase,
        )

    if not exists:
        return _degraded(tool_name=tool_name, reason="snapshot_missing", path=display_path, phase=phase)
    old_string = arguments.get("oldString")
    new_string = arguments.get("newString")
    if not isinstance(old_string, str):
        return _degraded(tool_name=tool_name, reason="missing_old_string", path=display_path, phase=phase)
    if not isinstance(new_string, str):
        return _degraded(tool_name=tool_name, reason="missing_new_string", path=display_path, phase=phase)
    try:
        if len(old_string.encode("utf-8")) > PREVIEW_SNAPSHOT_MAX_BYTES or len(new_string.encode("utf-8")) > PREVIEW_SNAPSHOT_MAX_BYTES:
            return _degraded(tool_name=tool_name, reason="proposed_content_too_large", path=display_path, phase=phase)
    except UnicodeError:
        return _degraded(tool_name=tool_name, reason="content_unreadable", path=display_path, phase=phase)
    if old_string == new_string:
        return _degraded(tool_name=tool_name, reason="no_op", path=display_path, phase=phase)
    match_count = before.count(old_string)
    if match_count == 0:
        return _degraded(tool_name=tool_name, reason="old_string_not_found", path=display_path, phase=phase)
    replace_all = arguments.get("replaceAll", False) is True
    if not replace_all and match_count > 1:
        return _degraded(tool_name=tool_name, reason="ambiguous_match", path=display_path, phase=phase)
    after = before.replace(old_string, new_string) if replace_all else before.replace(old_string, new_string, 1)
    try:
        if len(after.encode("utf-8")) > PREVIEW_SNAPSHOT_MAX_BYTES:
            return _degraded(tool_name=tool_name, reason="proposed_content_too_large", path=display_path, phase=phase)
    except UnicodeError:
        return _degraded(tool_name=tool_name, reason="content_unreadable", path=display_path, phase=phase)
    return _finish_diff_payload(
        _diff_payload(path=display_path, before=before, after=after, phase=phase, incomplete=incomplete),
        tool_name=tool_name,
        phase=phase,
    )


def _preview_multi_edit(*, workspace: Path, arguments: Mapping[str, object], phase: Literal["partial", "final"]) -> dict[str, object]:
    resolved = _resolve_preview_path(workspace=workspace, raw_path=arguments.get("path"))
    if isinstance(resolved, dict):
        return _finish_diff_payload(resolved, tool_name="multi_edit", phase=phase)
    candidate, display_path = resolved
    snapshot = _read_snapshot(candidate)
    if isinstance(snapshot, str):
        return _degraded(tool_name="multi_edit", reason=snapshot, path=display_path, phase=phase)
    before, exists = snapshot
    if not exists:
        return _degraded(tool_name="multi_edit", reason="snapshot_missing", path=display_path, phase=phase)
    raw_edits = arguments.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        return _degraded(tool_name="multi_edit", reason="missing_edits", path=display_path, phase=phase)
    if len(raw_edits) > PREVIEW_MAX_EDITS:
        return _degraded(tool_name="multi_edit", reason="too_many_edits", path=display_path, phase=phase)
    after = before
    for item in raw_edits:
        if not isinstance(item, Mapping):
            return _degraded(tool_name="multi_edit", reason="invalid_edit_entry", path=display_path, phase=phase)
        old_string = item.get("oldString")
        new_string = item.get("newString")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return _degraded(tool_name="multi_edit", reason="incomplete_edit_entry", path=display_path, phase=phase)
        try:
            if len(old_string.encode("utf-8")) > PREVIEW_SNAPSHOT_MAX_BYTES or len(new_string.encode("utf-8")) > PREVIEW_SNAPSHOT_MAX_BYTES:
                return _degraded(tool_name="multi_edit", reason="proposed_content_too_large", path=display_path, phase=phase)
        except UnicodeError:
            return _degraded(tool_name="multi_edit", reason="content_unreadable", path=display_path, phase=phase)
        if old_string not in after:
            return _degraded(tool_name="multi_edit", reason="old_string_not_found", path=display_path, phase=phase)
        after = after.replace(old_string, new_string) if item.get("replaceAll") is True else after.replace(old_string, new_string, 1)
        try:
            if len(after.encode("utf-8")) > PREVIEW_SNAPSHOT_MAX_BYTES:
                return _degraded(tool_name="multi_edit", reason="proposed_content_too_large", path=display_path, phase=phase)
        except UnicodeError:
            return _degraded(tool_name="multi_edit", reason="content_unreadable", path=display_path, phase=phase)
    return _finish_diff_payload(
        _diff_payload(path=display_path, before=before, after=after, phase=phase),
        tool_name="multi_edit",
        phase=phase,
    )


def _preview_patch(*, workspace: Path, arguments: Mapping[str, object], phase: Literal["partial", "final"], incomplete: bool) -> dict[str, object]:
    patch = arguments.get("patch")
    if not isinstance(patch, str) or not patch:
        return _degraded(tool_name="apply_patch", reason="missing_patch", phase=phase)
    try:
        if len(patch.encode("utf-8")) > PREVIEW_SNAPSHOT_MAX_BYTES:
            return _degraded(tool_name="apply_patch", reason="proposed_content_too_large", phase=phase)
    except UnicodeError:
        return _degraded(tool_name="apply_patch", reason="content_unreadable", phase=phase)
    # Parsing the existing patch metadata is read-only and gives clients safe
    # target identity. Applying it is intentionally left to the approved tool.
    try:
        from ..tools.apply_patch import _changes_from_patch

        changes = _changes_from_patch(patch)
    except Exception:
        changes = []
    output_paths = [str(item.get("path")) for item in changes if isinstance(item, Mapping) and isinstance(item.get("path"), str)]
    all_paths: list[str] = []
    for item in changes:
        if not isinstance(item, Mapping):
            continue
        for key in ("old_path", "path"):
            raw_path = item.get(key)
            if isinstance(raw_path, str) and raw_path not in all_paths:
                all_paths.append(raw_path)
    if not output_paths:
        return _degraded(tool_name="apply_patch", reason="patch_targets_unidentified", phase=phase)
    safe_paths: list[str] = []
    for raw_path in all_paths[:PREVIEW_MAX_PATHS]:
        resolved = _resolve_preview_path(workspace=workspace, raw_path=raw_path)
        if isinstance(resolved, dict):
            return _finish_diff_payload(resolved, tool_name="apply_patch", phase=phase)
        _candidate, display_path = resolved
        if raw_path in output_paths:
            safe_paths.append(display_path)
    if len(all_paths) > PREVIEW_MAX_PATHS:
        return _degraded(tool_name="apply_patch", reason="too_many_patch_targets", phase=phase)
    bounded_patch, patch_truncated = _bounded_diff(patch)
    return {
        "schema_version": 1,
        "phase": phase,
        "live_only": phase == "partial",
        "tool": "apply_patch",
        "status": "ready",
        "bounded": True,
        "paths": safe_paths,
        "diff": bounded_patch,
        "truncated": patch_truncated or incomplete,
        "format": "patch",
    }


def build_tool_call_preview(
    *,
    workspace: Path,
    tool_name: str,
    arguments: Mapping[str, object],
    phase: Literal["partial", "final"] = "final",
    incomplete: bool = False,
) -> dict[str, object] | None:
    """Build a bounded, read-only projection for a write-like tool call."""
    if tool_name not in WRITE_PREVIEW_TOOLS:
        return None
    try:
        if tool_name in {"write", "edit"}:
            return _preview_single_file(
                workspace=workspace,
                tool_name=tool_name,
                arguments=arguments,
                phase=phase,
                incomplete=incomplete,
            )
        if tool_name == "multi_edit":
            return _preview_multi_edit(workspace=workspace, arguments=arguments, phase=phase)
        return _preview_patch(workspace=workspace, arguments=arguments, phase=phase, incomplete=incomplete)
    except OSError, RuntimeError, UnicodeError, ValueError, TypeError:
        return _degraded(tool_name=tool_name, reason="preview_unavailable", phase=phase)


def build_partial_tool_call_preview(
    *,
    workspace: Path,
    tool_name: str,
    argument_text: str,
    parsed_arguments: Mapping[str, object] | None = None,
) -> dict[str, object] | None:
    """Decode only known fields from a streamed argument prefix and preview it."""
    bounded_text, was_bounded = _utf8_prefix(argument_text, max_bytes=PREVIEW_SNAPSHOT_MAX_BYTES)
    partial = _partial_arguments(bounded_text, parsed_arguments)
    return build_tool_call_preview(
        workspace=workspace,
        tool_name=tool_name,
        arguments=partial.values,
        phase="partial",
        incomplete=partial.incomplete or was_bounded,
    )


__all__ = [
    "PREVIEW_DIFF_MAX_CHARS",
    "PREVIEW_SNAPSHOT_MAX_BYTES",
    "WRITE_PREVIEW_TOOLS",
    "build_partial_tool_call_preview",
    "build_tool_call_preview",
]
