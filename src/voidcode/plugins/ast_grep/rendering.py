"""Match rendering, projection budgets, and artifact-backed output for ast_grep."""

from __future__ import annotations

import json
from typing import Any

from voidcode.capabilities.tool_errors import raise_tool_diagnostic
from voidcode.sdk.services import ArtifactReference, ArtifactServices
from voidcode.sdk.tools import TOOL_OUTPUT_BYTES, OutputBounds, TextOutput

_MATCH_PROJECTION_BUDGET_BYTES = TOOL_OUTPUT_BYTES - 4 * 1024


def _render_single_match(match: dict[str, Any], *, rewrite: str | None = None) -> str:
    file = match.get("file", "<unknown>")
    text = match.get("text", "")
    range_value = match.get("range")
    start = range_value.get("start") if isinstance(range_value, dict) else None
    if isinstance(start, dict):
        line, column = start.get("line"), start.get("column")
        location = f"{file}:{line + 1}:{column + 1}" if isinstance(line, int) and isinstance(column, int) else str(file)
    else:
        location = str(file)
    lines = [f"{location}: {text}"]
    if rewrite is not None and match.get("replacement") is not None:
        lines.append(f"  -> {match['replacement']}")
    return "\n".join(lines)


def _match_body_entry(match: dict[str, Any]) -> dict[str, Any]:
    raw_path = match.get("file")
    path = raw_path if isinstance(raw_path, str) else "<unknown>"
    item: dict[str, Any] = {"file": path}
    range_value = match.get("range")
    start = range_value.get("start") if isinstance(range_value, dict) else None
    if isinstance(start, dict):
        line = start.get("line")
        column = start.get("column")
        if isinstance(line, int):
            item["line"] = line + 1
        if isinstance(column, int):
            item["column"] = column + 1
    return item


def _select_matches_for_budget(
    matches: list[dict[str, Any]],
    *,
    header_text: str,
    rewrite: str | None = None,
    budget_bytes: int = _MATCH_PROJECTION_BUDGET_BYTES,
) -> tuple[list[dict[str, Any]], str, bool]:
    preview: list[dict[str, Any]] = []
    rendered_lines: list[str] = []
    used_bytes = len(header_text.encode("utf-8"))
    truncated = False
    for match in matches:
        rendered = _render_single_match(match, rewrite=rewrite)
        entry = _match_body_entry(match)
        item_bytes = len(rendered.encode("utf-8")) + 1 + len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 2
        if used_bytes + item_bytes > budget_bytes:
            truncated = True
            break
        used_bytes += item_bytes
        rendered_lines.append(rendered)
        preview.append(entry)
    return preview, "\n".join(rendered_lines), truncated


def _match_result_output(
    output: str,
    native_jsonl: str,
    *,
    services: ArtifactServices,
    truncated: bool,
    artifact: ArtifactReference | None = None,
) -> tuple[TextOutput, ArtifactReference | None]:
    if artifact is None and native_jsonl.strip():
        try:
            artifact = services.spill_artifact(native_jsonl, media_type="text/x-ndjson; charset=utf-8")
        except (OSError, ValueError) as exc:
            raise_tool_diagnostic(
                message=f"ast-grep match output could not be stored as an artifact: {exc}",
                error_kind="tool_environment_error",
                reason="process_capture_failed",
                retry_guidance="Retry the operation; if it persists, check the artifact store.",
                details={"error": str(exc)},
            )
    prefix = f"Full native AST match JSONL artifact: {artifact.uri}.\n" if artifact is not None else ""
    return (
        TextOutput(
            prefix + output,
            bounds=OutputBounds(truncated=truncated, partial=truncated, reference=artifact, source="ast-grep:json-stream"),
        ),
        artifact,
    )


def _with_match_counts(
    preview: list[dict[str, Any]],
    *,
    total_count: int,
    truncated: bool,
) -> dict[str, Any]:
    return {
        "matches": preview,
        "match_count": total_count,
        "matches_truncated": truncated,
        "truncated": truncated,
    }


def _warnings_block(warnings: tuple[str, ...]) -> str:
    if not warnings:
        return ""
    return "\nWarnings:\n" + "\n".join(f"- {line}" for line in warnings)


def _search_budget(
    matches: list[dict[str, Any]],
    *,
    limit: int,
    header_text: str,
    rewrite: str | None = None,
) -> tuple[list[dict[str, Any]], str, bool]:
    selected = matches[:limit]
    preview, rendered, budget_truncated = _select_matches_for_budget(selected, header_text=header_text, rewrite=rewrite)
    return preview, rendered, len(selected) < len(matches) or budget_truncated


__all__ = [
    "_MATCH_PROJECTION_BUDGET_BYTES",
    "_match_body_entry",
    "_match_result_output",
    "_render_single_match",
    "_search_budget",
    "_select_matches_for_budget",
    "_warnings_block",
    "_with_match_counts",
]
