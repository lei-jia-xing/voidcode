from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator

from voidcode.capabilities.files.errors import raise_tool_diagnostic
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ArtifactReference, PluginServices, ProcessResult
from voidcode.sdk.tools import (
    OpaqueToolBody,
    OutputBounds,
    ResourceChange,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolSuccess,
)


class AstGrepArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["search", "preview", "replace"]
    pattern: str
    path: str
    rewrite: str | None = None
    lang: str | None = None
    apply: bool = False
    expectedHash: str | None = None

    @field_validator("pattern", "path", "lang")
    @classmethod
    def _nonempty_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value


def _resolve_candidate(*, workspace: Path, path_text: str) -> tuple[Path, str]:
    workspace_root = workspace.resolve()
    requested_path = Path(path_text)
    candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
    if not candidate.exists():
        raise ValueError(f"ast_grep target does not exist: {path_text}")
    display_path = candidate.as_posix() if not candidate.is_relative_to(workspace_root) else candidate.relative_to(workspace_root).as_posix()
    return candidate, display_path


def _parse_stream_output(stdout: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ast-grep returned invalid JSON stream output: {stripped}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("ast-grep match must be a JSON object")
        matches.append(parsed)
    return matches


def _render_matches(matches: list[dict[str, Any]], *, rewrite: str | None = None) -> str:
    rendered: list[str] = []
    for match in matches:
        file = match.get("file", "<unknown>")
        text = match.get("text", "")
        range_value = match.get("range")
        start = range_value.get("start") if isinstance(range_value, dict) else None
        if isinstance(start, dict):
            line, column = start.get("line"), start.get("column")
            location = f"{file}:{line + 1}:{column + 1}" if isinstance(line, int) and isinstance(column, int) else str(file)
        else:
            location = str(file)
        rendered.append(f"{location}: {text}")
        if rewrite is not None and match.get("replacement") is not None:
            rendered.append(f"  -> {match['replacement']}")
    return "\n".join(rendered)


_MATCH_PREVIEW_LIMIT = 50
_MATCH_PREVIEW_PATH_LIMIT = 256


def _match_body_preview(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for match in matches[:_MATCH_PREVIEW_LIMIT]:
        raw_path = match.get("file")
        path = raw_path if isinstance(raw_path, str) else "<unknown>"
        item: dict[str, Any] = {"file": path[:_MATCH_PREVIEW_PATH_LIMIT]}
        if len(path) > _MATCH_PREVIEW_PATH_LIMIT:
            item["file_truncated"] = True
        range_value = match.get("range")
        start = range_value.get("start") if isinstance(range_value, dict) else None
        if isinstance(start, dict):
            line = start.get("line")
            column = start.get("column")
            if isinstance(line, int):
                item["line"] = line + 1
            if isinstance(column, int):
                item["column"] = column + 1
        preview.append(item)
    return preview


def _match_result_output(
    output: str,
    native_jsonl: str,
    *,
    services: PluginServices,
    truncated: bool,
    artifact: ArtifactReference | None = None,
) -> tuple[TextOutput, ArtifactReference]:
    if artifact is None:
        artifact = services.spill_artifact(native_jsonl, media_type="text/x-ndjson; charset=utf-8")
    return (
        TextOutput(
            f'Full native AST match JSONL: read(path="{artifact.uri}").\n{output}',
            bounds=OutputBounds(truncated=truncated, partial=truncated, reference=artifact, source="ast-grep:json-stream"),
        ),
        artifact,
    )


def _with_match_counts(matches: list[dict[str, Any]]) -> dict[str, Any]:
    match_count = len(matches)
    preview = _match_body_preview(matches)
    truncated = match_count > len(preview)
    return {
        "matches": preview,
        "match_count": match_count,
        "matches_truncated": truncated,
        "truncated": truncated,
    }


def _preflight_replace_targets(
    matches: list[dict[str, Any]],
    *,
    args: AstGrepArgs,
    workspace: Path,
    input_candidate: Path,
) -> tuple[dict[Path, str], dict[Path, bytes], dict[Path, str]]:
    display_paths: dict[Path, str] = {}
    original_bytes: dict[Path, bytes] = {}
    source_hashes: dict[Path, str] = {}
    target_scope = input_candidate.resolve()

    for match in matches:
        raw_path = match.get("file")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("ast_grep native match is missing its target file path")
        requested_path = Path(raw_path)
        candidate = (requested_path if requested_path.is_absolute() else workspace.resolve() / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace.resolve())
        display_path = candidate.as_posix() if is_external else candidate.relative_to(workspace.resolve()).as_posix()
        if not candidate.is_file():
            raise ValueError(f"ast_grep native match target is not a file: {raw_path}")
        if target_scope.is_dir():
            try:
                candidate.relative_to(target_scope)
            except ValueError as exc:
                raise ValueError(f"ast_grep native match escaped target directory: {raw_path}") from exc
        elif candidate != target_scope:
            raise ValueError(f"ast_grep native match escaped target file: {raw_path}")

        if candidate not in original_bytes:
            content = candidate.read_bytes()
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"ast_grep target is not UTF-8 text: {display_path}") from exc
            actual_hash = hashlib.sha256(content).hexdigest()
            display_paths[candidate] = display_path
            original_bytes[candidate] = content
            source_hashes[candidate] = actual_hash

    if args.expectedHash is not None:
        if len(source_hashes) != 1:
            raise ValueError("ast_grep expectedHash is only supported when the replacement targets one file")
        target, actual_hash = next(iter(source_hashes.items()))
        if args.expectedHash != actual_hash:
            display_path = display_paths[target]
            raise_tool_diagnostic(
                message=f"ast_grep replace rejected because {display_path} changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry.",
                details={"expected_hash": args.expectedHash, "actual_hash": actual_hash, "path": display_path},
            )

    return display_paths, original_bytes, source_hashes


def _run_ast_grep(*, cmd: list[str], workspace: Path, services: PluginServices, timeout_seconds: int = 30) -> ProcessResult:
    result = services.run_process(cmd, cwd=workspace.resolve(), timeout_seconds=timeout_seconds)
    if result.cancelled:
        raise ValueError("ast-grep was cancelled")
    if result.timed_out:
        raise ValueError(f"ast-grep timed out after {timeout_seconds}s")
    if result.truncated:
        raise ValueError("ast-grep output exceeded the process capture limit; narrow the target or pattern")
    return result


def _raise_on_process_failure(*, completed: ProcessResult, fallback_message: str) -> None:
    if completed.exit_code != 0:
        raise ValueError(completed.stderr.strip() or fallback_message)


def _is_no_match_result(completed: ProcessResult) -> bool:
    return completed.exit_code == 1 and not completed.stdout.strip() and not completed.stderr.strip()


def _run_preview_replace(
    *,
    pattern: str,
    rewrite: str,
    path_text: str,
    lang: str | None,
    workspace: Path,
    services: PluginServices,
    timeout_seconds: int = 30,
) -> tuple[str, list[dict[str, Any]], int, str]:
    _, relative_path = _resolve_candidate(workspace=workspace, path_text=path_text)
    preview_cmd = ["ast-grep", "run", "--json=stream", "-p", pattern, "-r", rewrite]
    if lang:
        preview_cmd.extend(["--lang", lang])
    preview_cmd.append(relative_path)

    completed = _run_ast_grep(cmd=preview_cmd, workspace=workspace, services=services, timeout_seconds=timeout_seconds)
    if _is_no_match_result(completed):
        matches: list[dict[str, Any]] = []
    else:
        _raise_on_process_failure(completed=completed, fallback_message="ast-grep replace failed")
        matches = _parse_stream_output(completed.stdout)

    replacement_count = len(matches)
    return relative_path, matches, replacement_count, completed.stdout


# ── Unified AstGrepTool (merged search/preview/replace) ─────────────────────


# Read coverage: results here expose match excerpts (matched node text) and unified diff
# hunks, never a complete file or line window, so ast_grep declares no read observations;
# only `read` grants the lines a later write or AST-rewrite guard may rely on. Replacement keeps
# its own hash-bound preflight against the hashes the session observed.
class AstGrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="ast_grep",
        icon=Icon(glyph="⌕", fallback="A"),
        description=(
            "Structural code search and rewrite with ast-grep. Search and preview are read operations; replace mutates files and requires approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["search", "preview", "replace"], "description": "AST operation to perform."},
                "pattern": {"type": "string", "minLength": 1, "description": "ast-grep pattern."},
                "path": {"type": "string", "minLength": 1, "description": "Workspace-relative or absolute file or directory path."},
                "rewrite": {"type": "string"},
                "lang": {"type": "string"},
                "apply": {"type": "boolean"},
                "expectedHash": {"type": "string", "description": "Optional expected SHA-256 content hash of target file for replace mode."},
            },
            "required": ["mode", "pattern", "path"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
        path_argument_keys=("path",),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace()
        return self._invoke(call, workspace=workspace, timeout_seconds=30, services=services)

    def _invoke(self, call: ToolCall, *, workspace: Path, timeout_seconds: int, services: PluginServices) -> ToolResult:
        args = AstGrepArgs.model_validate(dict(call.arguments))
        if args.mode == "search":
            return self._invoke_search(args, services=services, workspace=workspace, timeout_seconds=timeout_seconds)
        if args.mode in ("preview", "replace"):
            return self._invoke_preview_replace(
                args,
                services=services,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
            )
        raise ValueError(f"unknown ast_grep mode: {args.mode}")

    def _invoke_search(
        self,
        args: AstGrepArgs,
        *,
        services: PluginServices,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult:
        _, relative_path = _resolve_candidate(workspace=workspace, path_text=args.path)
        cmd = ["ast-grep", "run", "--json=stream", "-p", args.pattern]
        if args.lang:
            cmd.extend(["--lang", args.lang])
        cmd.append(relative_path)

        completed = _run_ast_grep(cmd=cmd, workspace=workspace, services=services, timeout_seconds=timeout_seconds)
        if _is_no_match_result(completed):
            matches: list[dict[str, Any]] = []
        else:
            _raise_on_process_failure(completed=completed, fallback_message="ast-grep search failed")
            matches = _parse_stream_output(completed.stdout)

        match_count = len(matches)
        preview = matches[:_MATCH_PREVIEW_LIMIT]
        truncated = match_count > len(preview)
        output = f"Found {match_count} AST match(es) in {relative_path}"
        if truncated:
            output += f"\nShowing the first {len(preview)} matches."
        rendered_matches = _render_matches(preview)
        if rendered_matches:
            output += "\n" + rendered_matches
        output, artifact = _match_result_output(
            output,
            completed.stdout,
            services=services,
            truncated=truncated,
        )
        body: dict[str, Any] = {
            "path": relative_path,
            "pattern": args.pattern,
            "lang": args.lang,
            "mode": "search",
            "timeout_seconds": timeout_seconds,
            **_with_match_counts(matches),
        }
        return ToolSuccess(
            tool_name=self.definition.name,
            output=output,
            body=OpaqueToolBody(body),
        )

    def _invoke_preview_replace(
        self,
        args: AstGrepArgs,
        *,
        services: PluginServices,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult:
        if args.mode == "replace" and not args.apply:
            raise ValueError("ast_grep replace mode requires apply=True")
        rewrite = args.rewrite
        if rewrite is None:
            raise ValueError(f"ast_grep {args.mode} mode requires rewrite")

        relative_path, matches, replacement_count, native_jsonl = _run_preview_replace(
            pattern=args.pattern,
            rewrite=rewrite,
            path_text=args.path,
            lang=args.lang,
            services=services,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
        preview_matches = matches[:_MATCH_PREVIEW_LIMIT]
        truncated = replacement_count > len(preview_matches)

        if args.mode == "preview":
            output_text = f"Previewed {replacement_count} AST replacement(s) in {relative_path}"
            if truncated:
                output_text += f"\nShowing the first {len(preview_matches)} matches."
            rendered_matches = _render_matches(preview_matches, rewrite=rewrite)
            if rendered_matches:
                output_text += "\n" + rendered_matches
            output, artifact = _match_result_output(
                output_text,
                native_jsonl,
                services=services,
                truncated=truncated,
            )
            body: dict[str, Any] = {
                "path": relative_path,
                "pattern": args.pattern,
                "rewrite": args.rewrite,
                "lang": args.lang,
                "replacement_count": replacement_count,
                **_with_match_counts(matches),
                "applied": False,
                "mode": "preview",
                "timeout_seconds": timeout_seconds,
            }
            return ToolSuccess(
                tool_name=self.definition.name,
                output=output,
                body=OpaqueToolBody(body),
            )

        candidate, _ = _resolve_candidate(workspace=workspace, path_text=args.path)
        display_paths, original_bytes, source_hashes = _preflight_replace_targets(
            matches,
            args=args,
            workspace=workspace,
            input_candidate=candidate,
        )
        ordered_targets = sorted(display_paths, key=lambda target: display_paths[target])
        for target in ordered_targets:
            actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual_hash != source_hashes[target]:
                display_path = display_paths[target]
                raise_tool_diagnostic(
                    message=f"ast_grep replace rejected because {display_path} changed after preview (stale edit).",
                    error_kind="stale_edit",
                    reason="content_hash_mismatch",
                    retry_guidance="Preview the rewrite again against the current file contents before retrying.",
                    details={"expected_hash": source_hashes[target], "actual_hash": actual_hash, "path": display_path},
                )

        _, artifact = _match_result_output(
            "",
            native_jsonl,
            services=services,
            truncated=truncated,
        )
        apply_paths = [display_paths[target] for target in ordered_targets]
        if apply_paths:
            apply_cmd = ["ast-grep", "run", "-p", args.pattern, "-r", rewrite]
            if args.lang:
                apply_cmd.extend(["--lang", args.lang])
            apply_cmd.extend(["-U", *apply_paths])
            completed = _run_ast_grep(cmd=apply_cmd, workspace=workspace, services=services, timeout_seconds=timeout_seconds)
            if not _is_no_match_result(completed):
                _raise_on_process_failure(completed=completed, fallback_message="ast-grep replace failed")

        file_results: list[dict[str, Any]] = []
        for target in ordered_targets:
            final_bytes = target.read_bytes()
            final_content = final_bytes.decode("utf-8")
            display_path = display_paths[target]
            diff = "".join(
                difflib.unified_diff(
                    original_bytes[target].decode("utf-8").splitlines(keepends=True),
                    final_content.splitlines(keepends=True),
                    fromfile=f"a/{display_path}",
                    tofile=f"b/{display_path}",
                )
            )
            file_results.append(
                {
                    "path": display_path,
                    "source_hash": source_hashes[target],
                    "byte_count": len(final_bytes),
                    "content_hash": hashlib.sha256(final_bytes).hexdigest(),
                    "diff": diff,
                }
            )
        output_text = f"Applied {replacement_count} AST replacement(s) in {relative_path}"
        if len(file_results) > 1:
            output_text += f" across {len(file_results)} files."
            output_text += "\n" + "\n".join(f"{file['path']}: {file['byte_count']} bytes; SHA-256 {file['content_hash']}" for file in file_results)
        if truncated:
            output_text += f"\nShowing the first {len(preview_matches)} matches."
        rendered_matches = _render_matches(preview_matches, rewrite=rewrite)
        if rendered_matches:
            output_text += "\n" + rendered_matches
        output, artifact = _match_result_output(
            output_text,
            native_jsonl,
            services=services,
            truncated=truncated,
            artifact=artifact,
        )
        result_body: dict[str, Any] = {
            "path": relative_path,
            "pattern": args.pattern,
            "rewrite": args.rewrite,
            "lang": args.lang,
            "replacement_count": replacement_count,
            **_with_match_counts(matches),
            "applied": True,
            "mode": "replace",
            "timeout_seconds": timeout_seconds,
        }
        if len(file_results) == 1:
            result_body["source_hash"] = file_results[0]["source_hash"]
            result_body["final_hash"] = file_results[0]["content_hash"]
            result_body["diff"] = file_results[0]["diff"]
        elif file_results:
            result_body["files"] = file_results
        return ToolSuccess(
            tool_name=self.definition.name,
            output=output,
            body=OpaqueToolBody(result_body),
            changes=tuple(ResourceChange(str(item["path"])) for item in file_results),
        )
