from __future__ import annotations

import codecs
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from voidcode.capabilities.files._common import (
    DEFAULT_IGNORE_PATTERNS,
    MAX_MUTATION_CONTENT_BYTES,
    MAX_RESULT_LIMIT,
    PathEscapesWorkspaceError,
    full_match_path,
    has_hidden_component,
    is_default_ignored,
    resolve_workspace_path,
    validate_glob_patterns,
)
from voidcode.capabilities.files.mutation import literal_target, raise_symlink_target
from voidcode.capabilities.tool_errors import expected_failures, raise_tool_diagnostic
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import ArtifactReference, ArtifactServices, PluginServices, ProcessResult, ProcessServices
from voidcode.sdk.tools import (
    TOOL_OUTPUT_BYTES,
    Change,
    ConfirmedStop,
    OpaqueToolBody,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

__all__ = [
    "AstGrepArgs",
    "AstGrepReplaceArgs",
    "AstGrepReplaceTool",
    "AstGrepTool",
]

_DEFAULT_LIMIT: Final[int] = 200
#: ast-grep emits one JSON object per match; this leaves room for tens of thousands of
#: matches before the runtime has to return a partial artifact instead of complete data.
_MATCH_CAPTURE_BYTES: Final[int] = 8 * 1024 * 1024
_OUTLINE_CAPTURE_BYTES: Final[int] = 1024 * 1024
_APPLY_CAPTURE_BYTES: Final[int] = 1024 * 1024
_DIAGNOSTIC_CHARS: Final[int] = 4000
_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")
_ERROR_NODE_WARNING = "Pattern contains an ERROR node"
_MATCH_PROJECTION_BUDGET_BYTES = TOOL_OUTPUT_BYTES - 4 * 1024


class AstGrepArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["search", "preview"]
    pattern: str
    path: str
    rewrite: str | None = None
    lang: str | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    include_hidden: bool = True
    respect_gitignore: bool = False
    limit: int = Field(default=_DEFAULT_LIMIT, ge=1, le=MAX_RESULT_LIMIT)

    @field_validator("pattern", "path", "lang")
    @classmethod
    def _nonempty_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    _glob_patterns = field_validator("include", "exclude", mode="after")(validate_glob_patterns)


class AstGrepReplaceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str
    rewrite: str
    path: str
    lang: str | None = None
    apply: Literal[True]
    expectedHash: str
    include_hidden: bool = True
    respect_gitignore: bool = False

    @field_validator("pattern", "path", "lang")
    @classmethod
    def _nonempty_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("expectedHash")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if _SHA256_TEXT.fullmatch(value) is None:
            raise ValueError("expectedHash must be a lowercase 64-character SHA-256 hex digest")
        return value


@dataclass(frozen=True, slots=True)
class _ReplaceTarget:
    path: Path
    display_path: str
    source_hash: str
    byte_count: int
    original_text: str | None


def _short(value: str, *, limit: int = _DIAGNOSTIC_CHARS) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _invalid_path(tool_name: str, path_text: str, exc: BaseException) -> None:
    raise_tool_diagnostic(
        message=f"{tool_name} could not resolve the target path {path_text!r}: {exc}",
        error_kind="tool_input_mismatch",
        reason="invalid_path",
        retry_guidance="Retry with a valid file or directory path.",
        details={"path": path_text},
    )


def _resolve_search_candidate(*, workspace: Path, path_text: str) -> tuple[Path, str]:
    try:
        candidate, display_path = resolve_workspace_path(workspace, path_text)
    except (OSError, RuntimeError, ValueError) as exc:
        _invalid_path("ast_grep", path_text, exc)
    if not candidate.exists():
        raise_tool_diagnostic(
            message=f"ast_grep target does not exist: {path_text}",
            error_kind="tool_input_mismatch",
            reason="target_not_found",
            retry_guidance="Retry with an existing file or directory path.",
            details={"path": path_text},
        )
    if not candidate.is_file() and not candidate.is_dir():
        raise_tool_diagnostic(
            message=f"ast_grep target is not a regular file or directory: {path_text}",
            error_kind="tool_input_mismatch",
            reason="not_a_regular_file",
            retry_guidance="Retry with a regular file or directory path.",
            details={"path": path_text},
        )
    return candidate, display_path


def _resolve_replace_candidate(*, workspace: Path, path_text: str) -> tuple[Path, str]:
    if literal_target(workspace, path_text).is_symlink():
        raise_symlink_target("ast_grep_replace", path_text)
    try:
        candidate, display_path = resolve_workspace_path(workspace, path_text, allow_outside_workspace=False)
    except PathEscapesWorkspaceError:
        raise_tool_diagnostic(
            message=f"ast_grep_replace may only modify paths inside the workspace: {path_text}",
            error_kind="tool_input_mismatch",
            reason="outside_workspace",
            retry_guidance="Retry with a workspace-relative file; read, grep, and ast_grep search may still inspect external files.",
            details={"path": path_text},
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _invalid_path("ast_grep_replace", path_text, exc)
    if not candidate.is_file():
        raise_tool_diagnostic(
            message=f"ast_grep_replace only supports a single regular file inside the workspace: {path_text}",
            error_kind="tool_input_mismatch",
            reason="not_a_regular_file",
            retry_guidance="Retry with one workspace file; directory and multi-file replacement are not supported.",
            details={"path": path_text},
        )
    return candidate, display_path


def _raise_stale_edit(display_path: str, *, expected_hash: str, actual_hash: str, after_preview: bool) -> None:
    message = (
        f"ast_grep replace rejected because {display_path} changed after preview (stale edit)."
        if after_preview
        else f"ast_grep replace rejected because {display_path} changed since it was read (stale edit)."
    )
    guidance = (
        "Preview the rewrite again against the current file contents before retrying."
        if after_preview
        else "Refresh the target file's SHA-256 content hash from a current file read, then retry."
    )
    raise_tool_diagnostic(
        message=message,
        error_kind="stale_edit",
        reason="content_hash_mismatch",
        retry_guidance=guidance,
        details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
    )


def _load_replace_target(path: Path, display_path: str) -> _ReplaceTarget:
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    retained: list[bytes] = []
    size = 0
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                decoder.decode(chunk)
                if size <= MAX_MUTATION_CONTENT_BYTES:
                    retained.append(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise_tool_diagnostic(
            message=f"ast_grep replace only supports UTF-8 text files: {display_path}",
            error_kind="tool_input_mismatch",
            reason="unsupported_encoding",
            retry_guidance="Retry with a UTF-8 text file, or use a binary-aware tool.",
            details={"path": display_path},
        )
    except OSError as exc:
        raise_tool_diagnostic(
            message=f"ast_grep replace failed while reading {display_path}: {exc}",
            error_kind="tool_environment_error",
            reason="resource_read_failed",
            retry_guidance="Retry the replacement; if the failure persists, check the file's permissions.",
            details={"path": display_path},
        )
    original_text = b"".join(retained).decode("utf-8") if size <= MAX_MUTATION_CONTENT_BYTES else None
    return _ReplaceTarget(path=path, display_path=display_path, source_hash=digest.hexdigest(), byte_count=size, original_text=original_text)


def _file_digest(path: Path) -> tuple[str, int] | None:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None
    return digest.hexdigest(), size


def _warning_lines(stderr: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in stderr.splitlines() if line.strip())


def _has_invalid_pattern_warning(stderr: str) -> bool:
    return any(_ERROR_NODE_WARNING in line for line in _warning_lines(stderr))


def _raise_invalid_pattern(stderr: str, *, pattern: str | None = None) -> None:
    details: dict[str, object] = {"stderr": _short(stderr)}
    if pattern is not None:
        details["pattern"] = pattern
    raise_tool_diagnostic(
        message="ast-grep could not parse the pattern as a valid AST pattern for the target language.",
        error_kind="tool_input_mismatch",
        reason="invalid_arguments",
        retry_guidance="Correct the pattern, or pass an explicit lang matching the target syntax.",
        details=details,
    )


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
    if _has_invalid_pattern_warning(stderr):
        _raise_invalid_pattern(stderr)
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


def _infer_language(
    *,
    display_path: str,
    workspace: Path,
    services: PluginServices,
    timeout_seconds: int,
    stage: str,
) -> str:
    completed = _run_ast_grep(
        cmd=["ast-grep", "outline", "--json=stream", "--", display_path],
        workspace=workspace,
        services=services,
        timeout_seconds=timeout_seconds,
        query=True,
        max_output_bytes=_OUTLINE_CAPTURE_BYTES,
    )
    if completed.cancelled:
        raise_tool_diagnostic(
            message=f"ast-grep language inference for {stage} was cancelled.",
            error_kind="tool_cancelled",
            reason="process_capture_failed",
            retry_guidance="Retry the operation if it was cancelled unintentionally.",
            details={"stage": stage},
        )
    if completed.timed_out:
        raise_tool_diagnostic(
            message=f"ast-grep language inference for {stage} timed out after {timeout_seconds}s.",
            error_kind="tool_execution_error",
            reason="search_io_error" if stage != "replace" else "edit_failed",
            retry_guidance="Pass lang explicitly and retry.",
            details={"stage": stage, "timeout_seconds": timeout_seconds},
        )
    if completed.truncated:
        raise_tool_diagnostic(
            message=f"ast-grep language inference for {stage} exceeded the capture limit; pass lang explicitly.",
            error_kind="tool_environment_error",
            reason="process_capture_failed",
            retry_guidance="Pass lang explicitly and retry.",
            details={"stage": stage},
        )
    if completed.exit_code != 0:
        _raise_on_process_failure(completed=completed, fallback_message="ast-grep outline failed", stage=stage)
    language = _parse_outline_language(completed.stdout)
    if language is None:
        raise_tool_diagnostic(
            message=f"ast-grep could not infer a language for {display_path}; pass lang explicitly.",
            error_kind="tool_input_mismatch",
            reason="invalid_arguments",
            retry_guidance="Retry with lang set to the target language (for example `python` or `typescript`).",
            details={"path": display_path, "stage": stage},
        )
    return language


def _resolved_language(
    args_lang: str | None,
    *,
    candidate: Path,
    display_path: str,
    workspace: Path,
    services: PluginServices,
    timeout_seconds: int,
    stage: str,
) -> str:
    if args_lang:
        return args_lang
    if candidate.is_dir():
        raise_tool_diagnostic(
            message=f"ast_grep {stage} requires lang when path is a directory: {display_path}",
            error_kind="tool_input_mismatch",
            reason="invalid_arguments",
            retry_guidance="Pass lang explicitly; ast-grep cannot disambiguate a structural pattern across languages.",
            details={"path": display_path, "stage": stage},
        )
    return _infer_language(
        display_path=display_path,
        workspace=workspace,
        services=services,
        timeout_seconds=timeout_seconds,
        stage=stage,
    )


def _ignore_flags(*, include_hidden: bool, respect_gitignore: bool) -> list[str]:
    flags: list[str] = []
    if include_hidden:
        flags.extend(["--no-ignore", "hidden"])
    if not respect_gitignore:
        flags.extend(["--no-ignore", "vcs"])
    flags.extend(["--no-ignore", "exclude", "--no-ignore", "global", "--no-ignore", "parent"])
    return flags


def _directory_globs() -> list[str]:
    return [f"!**/{name}/**" for name in sorted(DEFAULT_IGNORE_PATTERNS)]


def _file_matches_filters(path: Path, *, workspace: Path, include: list[str] | None, exclude: list[str] | None) -> bool:
    workspace_root = workspace.resolve()
    relative = path.relative_to(workspace_root).as_posix() if path.is_relative_to(workspace_root) else path.as_posix()
    if include and not any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in include):
        return False
    return not (exclude and any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in exclude))


def _match_resolved_path(match: dict[str, Any], *, workspace: Path) -> Path | None:
    raw_path = match.get("file")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    raw = Path(raw_path)
    return (raw if raw.is_absolute() else workspace.resolve() / raw).resolve()


def _surface_relative_path(match: dict[str, Any], *, workspace: Path, search_root: Path) -> str | None:
    resolved = _match_resolved_path(match, workspace=workspace)
    if resolved is None:
        return None
    workspace_root = workspace.resolve()
    if search_root.is_relative_to(workspace_root):
        return resolved.relative_to(workspace_root).as_posix() if resolved.is_relative_to(workspace_root) else resolved.as_posix()
    if search_root.is_dir():
        try:
            return resolved.relative_to(search_root.resolve()).as_posix()
        except ValueError:
            return resolved.as_posix()
    return resolved.as_posix()


def _below_search_root(match: dict[str, Any], *, workspace: Path, search_root: Path) -> str | None:
    resolved = _match_resolved_path(match, workspace=workspace)
    if resolved is None:
        return None
    try:
        return resolved.relative_to(search_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _filter_matches(
    matches: list[dict[str, Any]],
    *,
    workspace: Path,
    search_root: Path,
    include: list[str] | None,
    exclude: list[str] | None,
    include_hidden: bool,
) -> list[dict[str, Any]]:
    if search_root.is_file():
        return matches if _file_matches_filters(search_root, workspace=workspace, include=include, exclude=exclude) else []
    selected: list[dict[str, Any]] = []
    for match in matches:
        relative = _surface_relative_path(match, workspace=workspace, search_root=search_root)
        below_root = _below_search_root(match, workspace=workspace, search_root=search_root)
        if relative is None or below_root is None:
            continue
        if is_default_ignored(Path(below_root), is_directory=False):
            continue
        relative_path = Path(relative)
        if not include_hidden and has_hidden_component(relative_path):
            continue
        if include and not any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in include):
            continue
        if exclude and any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in exclude):
            continue
        selected.append(match)
    return selected


def _native_jsonl(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return ""
    return "\n".join(json.dumps(match, ensure_ascii=False) for match in matches) + "\n"


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
    pattern: str,
    stage: str,
) -> list[dict[str, Any]]:
    if _has_invalid_pattern_warning(completed.stderr):
        _raise_invalid_pattern(completed.stderr, pattern=pattern)
    if completed.exit_code == 1 and not completed.stdout.strip() and not completed.stderr.strip():
        return []
    _raise_on_process_failure(completed=completed, fallback_message=f"ast-grep {stage} failed", stage=stage)
    return _parse_stream_output(completed.stdout, stage=stage)


# ── Unified AstGrepTool (search/preview) ─────────────────────────────────────


class AstGrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="ast_grep",
        icon=Icon(glyph="⌕"),
        description="Structural code search and rewrite preview with ast-grep (read-only query).",
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["search", "preview"], "description": "AST operation to perform."},
                "pattern": {"type": "string", "minLength": 1, "description": "ast-grep pattern."},
                "path": {"type": "string", "minLength": 1, "description": "Workspace-relative or absolute file or directory path."},
                "rewrite": {"type": "string", "description": "Optional rewrite template (required for preview mode)."},
                "lang": {"type": "string", "description": "Language identifier; required for directory searches and previews."},
                "include": {"type": "array", "items": {"type": "string"}, "description": "Optional ast-grep --globs include patterns."},
                "exclude": {"type": "array", "items": {"type": "string"}, "description": "Optional ast-grep --globs exclude patterns."},
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files and directories (name starts with '.'); defaults to true, matching grep and glob.",
                },
                "respect_gitignore": {
                    "type": "boolean",
                    "description": (
                        "Respect the search root's VCS ignore stack; defaults to false, matching grep and glob. "
                        "Only VCS/root ignore sources are retained."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Maximum matches to return; defaults to 200. The full native JSONL stays in the artifact.",
                },
            },
            "required": ["mode", "pattern", "path"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        replay_policy="never",
        path_argument_keys=("path",),
    )

    @expected_failures
    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace()
        return self._invoke(call, workspace=workspace, timeout_seconds=30, services=services)

    def _invoke(self, call: ToolCall, *, workspace: Path, timeout_seconds: int, services: PluginServices) -> ToolResult:
        args = AstGrepArgs.model_validate(json_wire_object(call.arguments))
        if args.mode == "search":
            return self._invoke_search(args, services=services, workspace=workspace, timeout_seconds=timeout_seconds)
        return self._invoke_preview(args, services=services, workspace=workspace, timeout_seconds=timeout_seconds)

    def _invoke_search(
        self,
        args: AstGrepArgs,
        *,
        services: PluginServices,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult:
        candidate, relative_path = _resolve_search_candidate(workspace=workspace, path_text=args.path)
        lang = _resolved_language(
            args.lang,
            candidate=candidate,
            display_path=relative_path,
            workspace=workspace,
            services=services,
            timeout_seconds=timeout_seconds,
            stage="search",
        )
        native_jsonl = ""
        warnings: tuple[str, ...] = ()
        if candidate.is_file() and not _file_matches_filters(candidate, workspace=workspace, include=args.include, exclude=args.exclude):
            matches: list[dict[str, Any]] = []
        else:
            cmd = ["ast-grep", "run", "--json=stream", "-p", args.pattern, "--lang", lang]
            if candidate.is_dir():
                cmd.extend(_ignore_flags(include_hidden=args.include_hidden, respect_gitignore=args.respect_gitignore))
                for pattern in _directory_globs():
                    cmd.extend(["--globs", pattern])
            cmd.extend(["--", relative_path])
            completed = _run_ast_grep(
                cmd=cmd,
                workspace=workspace,
                services=services,
                timeout_seconds=timeout_seconds,
                query=True,
            )
            failure = _truncated_or_failed(
                completed,
                tool_name=self.definition.name,
                services=services,
                timeout_seconds=timeout_seconds,
                stage="search",
                timeout_reason="search_io_error",
            )
            if failure is not None:
                return failure
            matches = _filter_matches(
                _extract_matches(completed, pattern=args.pattern, stage="search"),
                workspace=workspace,
                search_root=candidate,
                include=args.include,
                exclude=args.exclude,
                include_hidden=args.include_hidden,
            )
            native_jsonl = _native_jsonl(matches)
            warnings = _warning_lines(completed.stderr)
        total_count = len(matches)
        header = f"Found {total_count} AST match(es) in {relative_path}"
        preview, rendered_matches, truncated = _search_budget(matches, limit=args.limit, header_text=header)
        if truncated:
            header += f"\nShowing the first {len(preview)} matches."
        if rendered_matches:
            header += "\n" + rendered_matches
        header += _warnings_block(warnings)
        output, _ = _match_result_output(header, native_jsonl, services=services, truncated=truncated)
        body: dict[str, Any] = {
            "path": relative_path,
            "pattern": args.pattern,
            "lang": lang,
            "mode": "search",
            "timeout_seconds": timeout_seconds,
            "include_hidden": args.include_hidden,
            "respect_gitignore": args.respect_gitignore,
            "include": args.include,
            "exclude": args.exclude,
            "limit": args.limit,
            "warnings": list(warnings),
            **_with_match_counts(preview, total_count=total_count, truncated=truncated),
        }
        return ToolSuccess(tool_name=self.definition.name, output=output, body=OpaqueToolBody(body))

    def _invoke_preview(
        self,
        args: AstGrepArgs,
        *,
        services: PluginServices,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult:
        rewrite = args.rewrite
        if rewrite is None:
            raise_tool_diagnostic(
                message="ast_grep preview mode requires rewrite.",
                error_kind="tool_input_mismatch",
                reason="invalid_arguments",
                retry_guidance="Pass rewrite with preview, or use search mode.",
                details={"path": args.path},
            )
        candidate, relative_path = _resolve_search_candidate(workspace=workspace, path_text=args.path)
        lang = _resolved_language(
            args.lang,
            candidate=candidate,
            display_path=relative_path,
            workspace=workspace,
            services=services,
            timeout_seconds=timeout_seconds,
            stage="preview",
        )
        native_jsonl = ""
        warnings: tuple[str, ...] = ()
        if candidate.is_file() and not _file_matches_filters(candidate, workspace=workspace, include=args.include, exclude=args.exclude):
            matches: list[dict[str, Any]] = []
        else:
            cmd = ["ast-grep", "run", "--json=stream", "-p", args.pattern, "-r", rewrite, "--lang", lang]
            if candidate.is_dir():
                cmd.extend(_ignore_flags(include_hidden=args.include_hidden, respect_gitignore=args.respect_gitignore))
                for pattern in _directory_globs():
                    cmd.extend(["--globs", pattern])
            cmd.extend(["--", relative_path])
            completed = _run_ast_grep(
                cmd=cmd,
                workspace=workspace,
                services=services,
                timeout_seconds=timeout_seconds,
                query=True,
            )
            failure = _truncated_or_failed(
                completed,
                tool_name=self.definition.name,
                services=services,
                timeout_seconds=timeout_seconds,
                stage="preview",
                timeout_reason="search_io_error",
            )
            if failure is not None:
                return failure
            matches = _filter_matches(
                _extract_matches(completed, pattern=args.pattern, stage="preview"),
                workspace=workspace,
                search_root=candidate,
                include=args.include,
                exclude=args.exclude,
                include_hidden=args.include_hidden,
            )
            native_jsonl = _native_jsonl(matches)
            warnings = _warning_lines(completed.stderr)
        total_count = len(matches)
        header = f"Previewed {total_count} AST replacement(s) in {relative_path}"
        preview, rendered_matches, truncated = _search_budget(matches, limit=args.limit, header_text=header, rewrite=rewrite)
        if truncated:
            header += f"\nShowing the first {len(preview)} matches."
        if rendered_matches:
            header += "\n" + rendered_matches
        header += _warnings_block(warnings)
        output, _ = _match_result_output(header, native_jsonl, services=services, truncated=truncated)
        body: dict[str, Any] = {
            "path": relative_path,
            "pattern": args.pattern,
            "rewrite": args.rewrite,
            "lang": lang,
            "replacement_count": total_count,
            "applied": False,
            "mode": "preview",
            "timeout_seconds": timeout_seconds,
            "include_hidden": args.include_hidden,
            "respect_gitignore": args.respect_gitignore,
            "include": args.include,
            "exclude": args.exclude,
            "limit": args.limit,
            "warnings": list(warnings),
            **_with_match_counts(preview, total_count=total_count, truncated=truncated),
        }
        return ToolSuccess(tool_name=self.definition.name, output=output, body=OpaqueToolBody(body))


# ── ast_grep_replace (workspace-only, single regular file) ───────────────────


class AstGrepReplaceTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="ast_grep_replace",
        icon=Icon(glyph="⌕"),
        description=(
            "Structural rewrite with ast-grep. Mutates one regular file inside the workspace and requires the "
            "current 64-character SHA-256 expectedHash; directory, multi-file, symlink, and external targets are refused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "description": "ast-grep pattern."},
                "rewrite": {"type": "string", "description": "Rewrite template for matched AST nodes."},
                "path": {"type": "string", "minLength": 1, "description": "One workspace-relative or absolute regular file."},
                "lang": {"type": "string", "description": "Optional language identifier; inferred from a file target when omitted."},
                "apply": {"type": "boolean", "enum": [True], "description": "Must be True; omitted or false is rejected."},
                "expectedHash": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                    "minLength": 64,
                    "maxLength": 64,
                    "description": "Current SHA-256 of the target file's bytes, from read or read(hash_only=true).",
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Accepted for surface parity with ast_grep; explicit file targets are always read.",
                },
                "respect_gitignore": {
                    "type": "boolean",
                    "description": "Accepted for surface parity with ast_grep; explicit file targets are always read.",
                },
            },
            "required": ["pattern", "rewrite", "path", "apply", "expectedHash"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
        path_argument_keys=("path",),
    )

    @expected_failures
    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace()
        return self._invoke(call, workspace=workspace, timeout_seconds=30, services=services)

    def _invoke(self, call: ToolCall, *, workspace: Path, timeout_seconds: int, services: PluginServices) -> ToolResult:
        args = AstGrepReplaceArgs.model_validate(json_wire_object(call.arguments))
        candidate, display_path = _resolve_replace_candidate(workspace=workspace, path_text=args.path)
        target = _load_replace_target(candidate, display_path)
        if args.expectedHash != target.source_hash:
            _raise_stale_edit(display_path, expected_hash=args.expectedHash, actual_hash=target.source_hash, after_preview=False)
        lang = _resolved_language(
            args.lang,
            candidate=candidate,
            display_path=display_path,
            workspace=workspace,
            services=services,
            timeout_seconds=timeout_seconds,
            stage="replace",
        )
        cmd = ["ast-grep", "run", "--json=stream", "-p", args.pattern, "-r", args.rewrite, "--lang", lang]
        cmd.extend(_ignore_flags(include_hidden=args.include_hidden, respect_gitignore=args.respect_gitignore))
        cmd.extend(["--", display_path])
        completed = _run_ast_grep(
            cmd=cmd,
            workspace=workspace,
            services=services,
            timeout_seconds=timeout_seconds,
            query=False,
        )
        failure = _truncated_or_failed(
            completed,
            tool_name=self.definition.name,
            services=services,
            timeout_seconds=timeout_seconds,
            stage="replace preview",
            timeout_reason="edit_failed",
        )
        if failure is not None:
            return failure
        matches = _extract_matches(completed, pattern=args.pattern, stage="replace preview")
        warnings = _warning_lines(completed.stderr)
        for match in matches:
            raw_path = match.get("file")
            if not isinstance(raw_path, str) or not raw_path:
                raise_tool_diagnostic(
                    message="ast-grep native match is missing its target file path.",
                    error_kind="tool_execution_error",
                    reason="edit_failed",
                    retry_guidance="Retry the replacement; if it persists, check the installed ast-grep version.",
                    details={"stage": "replace preview"},
                )
            raw = Path(raw_path)
            matched = (raw if raw.is_absolute() else workspace.resolve() / raw).resolve()
            if matched != candidate:
                raise_tool_diagnostic(
                    message=f"ast-grep native match escaped the single-file target: {raw_path}",
                    error_kind="tool_execution_error",
                    reason="edit_failed",
                    retry_guidance="Retry with a single workspace file whose path is the only match target.",
                    details={"path": display_path, "matched_path": raw_path},
                )
        current = _load_replace_target(candidate, display_path)
        if current.source_hash != target.source_hash:
            _raise_stale_edit(display_path, expected_hash=target.source_hash, actual_hash=current.source_hash, after_preview=True)
        if not matches:
            output_text = f"No AST replacements in {display_path}."
            output_text += _warnings_block(warnings)
            body: dict[str, Any] = {
                "path": display_path,
                "pattern": args.pattern,
                "rewrite": args.rewrite,
                "lang": lang,
                "replacement_count": 0,
                "applied": False,
                "mode": "replace",
                "timeout_seconds": timeout_seconds,
                "source_hash": target.source_hash,
                "final_hash": target.source_hash,
                "byte_count": target.byte_count,
                "warnings": list(warnings),
                "matches": [],
                "match_count": 0,
                "matches_truncated": False,
                "truncated": False,
            }
            return ToolSuccess(tool_name=self.definition.name, output=TextOutput(output_text), body=OpaqueToolBody(body))

        apply_cmd = ["ast-grep", "run", "-p", args.pattern, "-r", args.rewrite, "--lang", lang]
        apply_cmd.extend(_ignore_flags(include_hidden=args.include_hidden, respect_gitignore=args.respect_gitignore))
        apply_cmd.extend(["-U", "--", display_path])
        applied = _run_ast_grep(
            cmd=apply_cmd,
            workspace=workspace,
            services=services,
            timeout_seconds=timeout_seconds,
            query=False,
            max_output_bytes=_APPLY_CAPTURE_BYTES,
        )
        apply_warnings = _warning_lines(applied.stderr)
        if applied.cancelled or applied.timed_out or applied.truncated or applied.exit_code != 0 or _has_invalid_pattern_warning(applied.stderr):
            return self._apply_failure(
                applied,
                target=target,
                display_path=display_path,
                services=services,
                timeout_seconds=timeout_seconds,
                pattern=args.pattern,
            )
        final = _load_replace_target(candidate, display_path)
        if final.source_hash == target.source_hash:
            message = f"ast-grep reported success but {display_path} is unchanged."
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message),
                diagnostics=ToolDiagnostics(
                    kind="no_op",
                    summary=message,
                    details={"path": display_path, "source_hash": target.source_hash},
                    guidance="Check that the pattern actually matches and the rewrite can change the target, then retry.",
                ),
            )
        diff: str | None = None
        if target.original_text is not None and final.original_text is not None:
            diff = "".join(
                difflib.unified_diff(
                    target.original_text.splitlines(keepends=True),
                    final.original_text.splitlines(keepends=True),
                    fromfile=f"a/{display_path}",
                    tofile=f"b/{display_path}",
                )
            )
        reported = _parse_applied_count(applied.stdout)
        if reported is not None and reported != len(matches):
            apply_warnings += (f"ast-grep reported {reported} applied change(s) but preview found {len(matches)} match(es).",)
        header = f"Applied {len(matches)} AST replacement(s) in {display_path}"
        preview, rendered_matches, budget_truncated = _select_matches_for_budget(matches, header_text=header, rewrite=args.rewrite)
        if budget_truncated:
            header += f"\nShowing the first {len(preview)} matches."
        if rendered_matches:
            header += "\n" + rendered_matches
        header += _warnings_block(warnings + apply_warnings)
        output, _ = _match_result_output(header, completed.stdout, services=services, truncated=budget_truncated)
        body = {
            "path": display_path,
            "pattern": args.pattern,
            "rewrite": args.rewrite,
            "lang": lang,
            "replacement_count": len(matches),
            "applied": True,
            "mode": "replace",
            "timeout_seconds": timeout_seconds,
            "source_hash": target.source_hash,
            "final_hash": final.source_hash,
            "byte_count": final.byte_count,
            "apply_reported_count": reported,
            "warnings": list(warnings + apply_warnings),
            **_with_match_counts(preview, total_count=len(matches), truncated=budget_truncated),
        }
        if diff is not None:
            body["diff"] = diff
        else:
            body["diff_omitted"] = f"target exceeds the {MAX_MUTATION_CONTENT_BYTES // 1024} KiB diff budget"
        return ToolSuccess(
            tool_name=self.definition.name,
            output=output,
            body=OpaqueToolBody(body),
            changes=(Change(display_path, content_hash=final.source_hash),),
        )

    def _apply_failure(
        self,
        applied: ProcessResult,
        *,
        target: _ReplaceTarget,
        display_path: str,
        services: PluginServices,
        timeout_seconds: int,
        pattern: str,
    ) -> ToolResult:
        state = _file_digest(target.path)
        changes: tuple[Change, ...] = ()
        partial_note = ""
        if state is not None and state[0] != target.source_hash:
            changes = (Change(display_path, content_hash=state[0]),)
            partial_note = f" The target may have been partially rewritten; current SHA-256 is {state[0]}."
        artifact = _spill_output(applied, services=services, media_type="text/plain; charset=utf-8")
        artifact_line = f" Captured output: {artifact.uri}." if artifact is not None else ""
        if applied.cancelled:
            message = f"ast-grep replace was cancelled after it may have started.{partial_note}{artifact_line}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message, bounds=OutputBounds(partial=True, reference=artifact, source="ast-grep:replace")),
                diagnostics=ToolDiagnostics(
                    kind="tool_cancelled",
                    summary=message,
                    details={"stage": "replace apply", "changed": bool(changes)},
                    guidance="Verify the file's current contents with read before retrying.",
                ),
                execution=ConfirmedStop(cancellation_signalled=True),
                changes=changes,
            )
        if applied.timed_out:
            message = f"ast-grep replace timed out after {timeout_seconds}s.{partial_note}{artifact_line}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message, bounds=OutputBounds(partial=True, reference=artifact, source="ast-grep:replace")),
                diagnostics=ToolDiagnostics(
                    kind="tool_execution_error",
                    summary=message,
                    details={"reason": "edit_failed", "stage": "replace apply", "timeout_seconds": timeout_seconds, "changed": bool(changes)},
                    guidance="Read the file to verify any partial rewrite, then retry with its current hash.",
                ),
                timeout_seconds=timeout_seconds,
                changes=changes,
            )
        if _has_invalid_pattern_warning(applied.stderr):
            message = f"ast-grep could not parse the pattern during replace.{partial_note}{artifact_line}"
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message, bounds=OutputBounds(partial=bool(changes), reference=artifact, source="ast-grep:replace")),
                diagnostics=ToolDiagnostics(
                    kind="tool_input_mismatch",
                    summary=message,
                    details={
                        "reason": "invalid_arguments",
                        "stage": "replace apply",
                        "changed": bool(changes),
                        "pattern": pattern,
                        "stderr": _short(applied.stderr),
                    },
                    guidance="Correct the pattern, or pass an explicit lang matching the target syntax.",
                ),
                changes=changes,
            )
        reason = "process_capture_failed" if applied.truncated else "edit_failed"
        detail = "output exceeded the capture limit" if applied.truncated else _short(applied.stderr.strip()) or "ast-grep replace failed"
        message = f"ast-grep replace failed: {detail}.{partial_note}{artifact_line}"
        return ToolFailure(
            tool_name=self.definition.name,
            error=message,
            output=TextOutput(message, bounds=OutputBounds(partial=True, reference=artifact, source="ast-grep:replace")),
            diagnostics=ToolDiagnostics(
                kind="tool_execution_error",
                summary=message,
                details={"reason": reason, "stage": "replace apply", "changed": bool(changes), "stderr": _short(applied.stderr)},
                guidance="Read the file to verify any partial rewrite, then retry with its current hash.",
            ),
            changes=changes,
        )
