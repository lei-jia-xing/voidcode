from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationInfo, field_validator

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    OpaqueToolBody,
    OutputBounds,
    OutputReference,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .guards import _coverage, enforce_read_before_write, enforce_seen_lines
from .repair import raise_tool_diagnostic

# ── Unified args model for merged AstGrepTool ───────────────────────────────


class AstGrepArgs(BaseModel):
    mode: str  # "search" | "preview" | "replace"
    pattern: str
    path: str
    rewrite: str | None = None
    lang: str | None = None
    apply: bool = False
    expectedHash: str | None = None

    @field_validator("mode", mode="after")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        if value not in ("search", "preview", "replace"):
            raise ValueError("mode must be one of search, preview, replace")
        return value

    @field_validator("pattern", mode="after")
    @classmethod
    def _validate_pattern(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must not be empty")
        return value

    @field_validator("path", mode="after")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be a non-empty string")
        return value

    @field_validator("lang", mode="after")
    @classmethod
    def _validate_lang(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("lang must not be empty")
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
        if isinstance(parsed, dict):
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
    artifact: dict[str, object] | None = None,
) -> tuple[TextOutput, dict[str, object]]:
    if artifact is None:
        reference = services.spill_artifact(native_jsonl, media_type="text/x-ndjson; charset=utf-8")
        uri = f"voidcode://artifact/{reference.sha256}"
        artifact = {
            "artifact_id": reference.sha256,
            "sha256": reference.sha256,
            "byte_count": reference.bytes,
            "content_type": reference.media_type,
            "mime": reference.media_type,
            "uri": uri,
            "kind": "ast_grep_matches",
            "status": "available",
            "source": "ast-grep:json-stream",
        }
    else:
        uri = f"voidcode://artifact/{artifact['artifact_id']}"
    return (
        TextOutput(
            f'Full native AST match JSONL: read(path="{uri}").\n{output}',
            bounds=OutputBounds(
                truncated=truncated,
                partial=truncated,
                reference=OutputReference(uri=uri, artifact=artifact),
            ),
        ),
        artifact,
    )


def _add_match_artifact(body: dict[str, Any], artifact: dict[str, object]) -> None:
    uri = f"voidcode://artifact/{artifact['artifact_id']}"
    body.update(body_artifact=artifact, artifacts=[artifact], response_reference=uri)


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
    context: ToolContext,
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
            enforce_read_before_write(
                context=context,
                tool_name="ast_grep",
                raw_path=raw_path,
                candidate=candidate,
                display_path=display_path,
            )
            actual_hash = hashlib.sha256(content).hexdigest()
            resolved_key = candidate.resolve().as_posix()
            _read_paths, read_lines, read_whole_files, _read_hash = _coverage(context)
            observed_hashes = {content_hash for observed_path, content_hash in read_lines if observed_path == resolved_key}
            observed_hashes.update(content_hash for observed_path, content_hash in read_whole_files if observed_path == resolved_key)
            if context.session_id is not None and actual_hash not in observed_hashes and len(observed_hashes) == 1:
                observed_hash = next(iter(observed_hashes))
                raise_tool_diagnostic(
                    message=f"ast_grep replace rejected because {display_path} changed since it was read (stale edit).",
                    error_kind="stale_edit",
                    reason="content_hash_mismatch",
                    retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry.",
                    details={"expected_hash": observed_hash, "actual_hash": actual_hash, "path": display_path},
                )
            display_paths[candidate] = display_path
            original_bytes[candidate] = content
            source_hashes[candidate] = actual_hash

        if context.session_id is not None:
            range_value = match.get("range")
            start = range_value.get("start") if isinstance(range_value, dict) else None
            end = range_value.get("end") if isinstance(range_value, dict) else None
            start_line = start.get("line") if isinstance(start, dict) else None
            end_line = end.get("line") if isinstance(end, dict) else None
            if not isinstance(start_line, int) or not isinstance(end_line, int) or start_line < 0 or end_line < 0:
                raise ValueError(f"ast_grep native match has an invalid range in {display_path}")
            enforce_seen_lines(
                context=context,
                tool_name="ast_grep",
                raw_path=raw_path,
                candidate=candidate,
                display_path=display_path,
                start_line=start_line + 1,
                end_line=end_line + 1,
                current_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
            )

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


def _run_ast_grep(*, cmd: list[str], workspace: Path, timeout_seconds: int = 30) -> ToolFailure | subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            cmd,
            cwd=workspace.resolve(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ToolFailure(
            tool_name=cmd[0].replace("-", "_"),
            error=f"ast-grep timed out after {timeout_seconds}s",
        )
    except OSError as exc:
        return ToolFailure(
            tool_name=cmd[0].replace("-", "_"),
            error=f"ast-grep not found or failed: {exc}",
        )
    return completed


def _raise_on_process_failure(*, completed: subprocess.CompletedProcess[str], fallback_message: str) -> None:
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or fallback_message)


def _is_no_match_result(completed: subprocess.CompletedProcess[str]) -> bool:
    return completed.returncode == 1 and not completed.stdout.strip() and not completed.stderr.strip()


def _run_preview_replace(
    *,
    pattern: str,
    rewrite: str,
    path_text: str,
    lang: str | None,
    workspace: Path,
    timeout_seconds: int = 30,
) -> ToolFailure | tuple[str, list[dict[str, Any]], int, str]:
    _, relative_path = _resolve_candidate(workspace=workspace, path_text=path_text)
    preview_cmd = ["ast-grep", "run", "--json=stream", "-p", pattern, "-r", rewrite]
    if lang:
        preview_cmd.extend(["--lang", lang])
    preview_cmd.append(relative_path)

    completed = _run_ast_grep(cmd=preview_cmd, workspace=workspace, timeout_seconds=timeout_seconds)
    if isinstance(completed, ToolFailure):
        return completed

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
    )

    def __init__(self) -> None:
        pass

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        workspace = context.require_workspace()
        return self._invoke(call, workspace=workspace, timeout_seconds=30, context=context, services=services)

    def _invoke(self, call: ToolCall, *, workspace: Path, timeout_seconds: int, context: ToolContext, services: PluginServices) -> ToolResult:
        args = self._validate_call(call)
        if args.mode == "search":
            return self._invoke_search(args, services=services, workspace=workspace, timeout_seconds=timeout_seconds)
        if args.mode in ("preview", "replace"):
            return self._invoke_preview_replace(
                args,
                context=context,
                services=services,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
            )
        raise ValueError(f"unknown ast_grep mode: {args.mode}")

    def _validate_call(self, call: ToolCall) -> AstGrepArgs:
        return AstGrepArgs.model_validate(
            {
                "mode": call.arguments.get("mode", "search"),
                "pattern": call.arguments.get("pattern"),
                "path": call.arguments.get("path"),
                "rewrite": call.arguments.get("rewrite"),
                "lang": call.arguments.get("lang"),
                "apply": call.arguments.get("apply", False),
                "expectedHash": call.arguments.get("expectedHash"),
            }
        )

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

        completed = _run_ast_grep(cmd=cmd, workspace=workspace, timeout_seconds=timeout_seconds)
        if isinstance(completed, ToolFailure):
            return ToolFailure(self.definition.name, error=completed.error)

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
        _add_match_artifact(body, artifact)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=output,
            body=OpaqueToolBody(body),
        )

    def _invoke_preview_replace(
        self,
        args: AstGrepArgs,
        *,
        context: ToolContext,
        services: PluginServices,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult:
        if args.mode == "replace" and not args.apply:
            raise ValueError("ast_grep replace mode requires apply=True")
        rewrite = args.rewrite
        if rewrite is None:
            raise ValueError(f"ast_grep {args.mode} mode requires rewrite")

        preview = _run_preview_replace(
            pattern=args.pattern,
            rewrite=rewrite,
            path_text=args.path,
            lang=args.lang,
            workspace=workspace,
            timeout_seconds=timeout_seconds,
        )
        if isinstance(preview, ToolFailure):
            return ToolFailure(self.definition.name, error=preview.error)
        relative_path, matches, replacement_count, native_jsonl = preview
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
            _add_match_artifact(body, artifact)
            return ToolSuccess(
                tool_name=self.definition.name,
                output=output,
                body=OpaqueToolBody(body),
            )

        candidate, _ = _resolve_candidate(workspace=workspace, path_text=args.path)
        display_paths, original_bytes, source_hashes = _preflight_replace_targets(
            matches,
            args=args,
            context=context,
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
            completed = _run_ast_grep(cmd=apply_cmd, workspace=workspace, timeout_seconds=timeout_seconds)
            if isinstance(completed, ToolFailure):
                return ToolFailure(self.definition.name, error=completed.error)
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
        _add_match_artifact(result_body, artifact)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=output,
            body=OpaqueToolBody(result_body),
        )
