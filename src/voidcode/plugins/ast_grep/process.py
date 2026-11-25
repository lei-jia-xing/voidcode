"""ast-grep process invocation, capture, parsing, and typed stop failures."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final, Literal

from voidcode.capabilities.tool_errors import raise_tool_diagnostic
from voidcode.sdk.services import ArtifactReference, ArtifactServices, PluginServices, ProcessResult, ProcessServices
from voidcode.sdk.tools import ConfirmedStop, OutputBounds, TextOutput, ToolDiagnostics, ToolFailure

#: ast-grep emits one JSON object per match; this leaves room for tens of thousands of
#: matches before the runtime has to return a partial artifact instead of complete data.
_MATCH_CAPTURE_BYTES: Final[int] = 8 * 1024 * 1024
_OUTLINE_CAPTURE_BYTES: Final[int] = 1024 * 1024
_APPLY_CAPTURE_BYTES: Final[int] = 1024 * 1024
_DIAGNOSTIC_CHARS: Final[int] = 4000


def _short(value: str, *, limit: int = _DIAGNOSTIC_CHARS) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _warning_lines(stderr: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in stderr.splitlines() if line.strip())


def _parse_stream_output(stdout: str, *, stage: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            raise_tool_diagnostic(
                message=f"ast-grep {stage} returned invalid JSON stream output.",
                error_kind="tool_execution_error",
                reason="process_capture_failed",
                retry_guidance="Retry the search; if it persists, check the installed ast-grep version.",
                details={"stage": stage, "line": _short(stripped)},
            )
        if not isinstance(parsed, dict):
            raise_tool_diagnostic(
                message=f"ast-grep {stage} returned a non-object JSON match.",
                error_kind="tool_execution_error",
                reason="process_capture_failed",
                retry_guidance="Retry the search; if it persists, check the installed ast-grep version.",
                details={"stage": stage, "line": _short(stripped)},
            )
        matches.append(parsed)
    return matches


def _parse_outline_language(stdout: str) -> str | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        if text.startswith("["):
            payload: object = json.loads(text)
            entries = payload if isinstance(payload, list) else []
        else:
            entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return None
    for entry in entries:
        if isinstance(entry, dict):
            language = entry.get("language")
            if isinstance(language, str) and language.strip():
                return language
    return None


def _parse_applied_count(stdout: str) -> int | None:
    matched = re.search(r"Applied\s+(\d+)\s+changes?", stdout, re.IGNORECASE)
    return int(matched.group(1)) if matched is not None else None


def _spill_output(
    completed: ProcessResult,
    *,
    services: ArtifactServices,
    media_type: str = "text/x-ndjson; charset=utf-8",
) -> ArtifactReference | None:
    if not completed.stdout:
        return None
    try:
        return services.spill_artifact(completed.stdout, media_type=media_type)
    except (OSError, ValueError) as exc:
        raise_tool_diagnostic(
            message=f"ast-grep captured output could not be stored as an artifact: {exc}",
            error_kind="tool_environment_error",
            reason="process_capture_failed",
            retry_guidance="Retry the operation; if it persists, check the artifact store.",
            details={"error": str(exc)},
        )


def _stop_failure(
    completed: ProcessResult,
    *,
    tool_name: str,
    services: ArtifactServices,
    timeout_seconds: int,
    stage: str,
    timeout_reason: Literal["search_io_error", "edit_failed"],
) -> ToolFailure | None:
    if not (completed.cancelled or completed.timed_out or completed.truncated):
        return None
    artifact = _spill_output(completed, services=services)
    artifact_line = f" Partial captured output: {artifact.uri}." if artifact is not None else ""
    if completed.cancelled:
        message = f"ast-grep {stage} was cancelled.{artifact_line}"
        return ToolFailure(
            tool_name=tool_name,
            error=message,
            output=TextOutput(message, bounds=OutputBounds(partial=True, reference=artifact, source=f"ast-grep:{stage}")),
            diagnostics=ToolDiagnostics(
                kind="tool_cancelled",
                summary=message,
                details={"stage": stage},
                guidance="Retry the operation if it was cancelled unintentionally.",
            ),
            execution=ConfirmedStop(cancellation_signalled=True),
        )
    if completed.timed_out:
        message = f"ast-grep {stage} timed out after {timeout_seconds}s.{artifact_line}"
        return ToolFailure(
            tool_name=tool_name,
            error=message,
            output=TextOutput(message, bounds=OutputBounds(partial=True, reference=artifact, source=f"ast-grep:{stage}")),
            diagnostics=ToolDiagnostics(
                kind="tool_execution_error",
                summary=message,
                details={"reason": timeout_reason, "stage": stage, "timeout_seconds": timeout_seconds},
                guidance="Narrow the target or pattern, then retry.",
            ),
            timeout_seconds=timeout_seconds,
        )
    message = f"ast-grep {stage} exceeded the process output capture limit; results are partial.{artifact_line}"
    return ToolFailure(
        tool_name=tool_name,
        error=message,
        output=TextOutput(message, bounds=OutputBounds(truncated=True, partial=True, reference=artifact, source=f"ast-grep:{stage}")),
        diagnostics=ToolDiagnostics(
            kind="tool_execution_error",
            summary=message,
            details={"reason": "process_capture_failed", "stage": stage},
            guidance="Narrow the target, add include/exclude filters, or lower limit, then retry.",
        ),
    )


def _run_ast_grep(
    *,
    cmd: list[str],
    workspace: Path,
    services: ProcessServices,
    timeout_seconds: int = 30,
    query: bool = False,
    max_output_bytes: int = _MATCH_CAPTURE_BYTES,
) -> ProcessResult:
    runner = services.run_query_process if query else services.run_process
    try:
        return runner(cmd, cwd=workspace.resolve(), timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)
    except (OSError, ValueError) as exc:
        raise_tool_diagnostic(
            message=f"ast-grep could not start: {exc}",
            error_kind="tool_environment_error",
            reason="process_start_failed",
            retry_guidance=(
                "Install ast-grep 0.50 or newer and ensure the `ast-grep` executable is on PATH. This plugin does not configure an executable path."
            ),
            details={"error": str(exc)},
        )


def _raise_on_process_failure(*, completed: ProcessResult, fallback_message: str, stage: str) -> None:
    if completed.exit_code == 0:
        return
    stderr = completed.stderr.strip()
    if completed.exit_code == 2:
        raise_tool_diagnostic(
            message=f"ast-grep rejected the {stage} arguments: {_short(stderr) or fallback_message}",
            error_kind="tool_input_mismatch",
            reason="invalid_arguments",
            retry_guidance="Check pattern, lang, and the target path, then retry.",
            details={"stage": stage, "stderr": _short(stderr)},
        )
    raise_tool_diagnostic(
        message=f"ast-grep {stage} failed: {_short(stderr) or fallback_message}",
        error_kind="tool_execution_error",
        reason="search_io_error" if stage != "replace" else "edit_failed",
        retry_guidance="Retry the operation; if it persists, check the installed ast-grep version and the target path.",
        details={"stage": stage, "stderr": _short(stderr)},
    )


def _truncated_or_failed(
    completed: ProcessResult,
    *,
    tool_name: str,
    services: PluginServices,
    timeout_seconds: int,
    stage: str,
    timeout_reason: Literal["search_io_error", "edit_failed"],
) -> ToolFailure | None:
    return _stop_failure(
        completed,
        tool_name=tool_name,
        services=services,
        timeout_seconds=timeout_seconds,
        stage=stage,
        timeout_reason=timeout_reason,
    )


def _extract_matches(
    completed: ProcessResult,
    *,
    stage: str,
) -> list[dict[str, Any]]:
    if completed.exit_code == 1 and not completed.stdout.strip() and not completed.stderr.strip():
        return []
    _raise_on_process_failure(completed=completed, fallback_message=f"ast-grep {stage} failed", stage=stage)
    return _parse_stream_output(completed.stdout, stage=stage)


__all__ = [
    "_APPLY_CAPTURE_BYTES",
    "_DIAGNOSTIC_CHARS",
    "_MATCH_CAPTURE_BYTES",
    "_OUTLINE_CAPTURE_BYTES",
    "_extract_matches",
    "_parse_applied_count",
    "_parse_outline_language",
    "_parse_stream_output",
    "_raise_on_process_failure",
    "_run_ast_grep",
    "_short",
    "_spill_output",
    "_stop_failure",
    "_truncated_or_failed",
    "_warning_lines",
]
