from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, field_validator

from ..core.tool_context import ToolContext
from ..security.path_policy import resolve_workspace_path
from ._pydantic_args import parse_tool_args, validate_non_empty
from .contracts import OpaqueToolBody, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess

# ── Unified args model for merged AstGrepTool ───────────────────────────────


class AstGrepArgs(BaseModel):
    mode: str  # "search" | "preview" | "replace"
    pattern: str
    path: str
    rewrite: str | None = None
    lang: str | None = None
    apply: bool = False

    @field_validator("mode", mode="after")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        if value not in ("search", "preview", "replace"):
            raise ValueError("mode must be one of search, preview, replace")
        return value

    _validate_pattern = field_validator("pattern", mode="after")(validate_non_empty)

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
    resolution = resolve_workspace_path(workspace=workspace, raw_path=path_text)
    if not resolution.candidate.exists():
        raise ValueError(f"ast_grep target does not exist: {path_text}")
    return resolution.candidate, resolution.relative_path


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
) -> ToolFailure | tuple[str, list[dict[str, Any]], int]:
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
    return relative_path, matches, replacement_count


# ── Unified AstGrepTool (merged search/preview/replace) ─────────────────────


class AstGrepTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="ast_grep",
        description=(
            "Structural code search and rewrite with ast-grep. Search and preview are read operations; replace mutates files and requires approval."
        ),
        input_schema={
            "mode": {"type": "string", "enum": ["search", "preview", "replace"], "description": "AST operation to perform."},
            "pattern": {"type": "string", "minLength": 1, "description": "ast-grep pattern."},
            "path": {"type": "string", "minLength": 1, "description": "File or directory path relative to workspace."},
            "rewrite": {"type": "string"},
            "lang": {"type": "string"},
            "apply": {"type": "boolean"},
            "required": ["mode", "pattern", "path"],
        },
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        return self._invoke(call, workspace=workspace, timeout_seconds=30)

    def invoke_with_runtime_timeout(self, call: ToolCall, *, context: ToolContext, timeout_seconds: int) -> ToolResult:
        return self._invoke(call, workspace=context.require_workspace(), timeout_seconds=timeout_seconds)

    def _invoke(self, call: ToolCall, *, workspace: Path, timeout_seconds: int) -> ToolResult:
        args = self._validate_call(call)
        if args.mode == "search":
            return self._invoke_search(args, workspace=workspace, timeout_seconds=timeout_seconds)
        if args.mode in ("preview", "replace"):
            return self._invoke_preview_replace(args, workspace=workspace, timeout_seconds=timeout_seconds)
        raise ValueError(f"unknown ast_grep mode: {args.mode}")

    def _validate_call(self, call: ToolCall) -> AstGrepArgs:
        return parse_tool_args(
            AstGrepArgs,
            {
                "mode": call.arguments.get("mode", "search"),
                "pattern": call.arguments.get("pattern"),
                "path": call.arguments.get("path"),
                "rewrite": call.arguments.get("rewrite"),
                "lang": call.arguments.get("lang"),
                "apply": call.arguments.get("apply", False),
            },
            tool_name=self.definition.name,
        )

    def _invoke_search(self, args: AstGrepArgs, *, workspace: Path, timeout_seconds: int) -> ToolResult:
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
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Found {match_count} AST match(es) in {relative_path}"),
            body=OpaqueToolBody(
                {
                    "path": relative_path,
                    "pattern": args.pattern,
                    "lang": args.lang,
                    "match_count": match_count,
                    "matches": matches,
                    "mode": "search",
                    "timeout_seconds": timeout_seconds,
                }
            ),
        )

    def _invoke_preview_replace(self, args: AstGrepArgs, *, workspace: Path, timeout_seconds: int) -> ToolResult:
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
        relative_path, matches, replacement_count = preview

        if args.mode == "preview":
            return ToolSuccess(
                tool_name=self.definition.name,
                output=TextOutput(f"Previewed {replacement_count} AST replacement(s) in {relative_path}"),
                body=OpaqueToolBody(
                    {
                        "path": relative_path,
                        "pattern": args.pattern,
                        "rewrite": args.rewrite,
                        "lang": args.lang,
                        "replacement_count": replacement_count,
                        "matches": matches,
                        "applied": False,
                        "mode": "preview",
                        "timeout_seconds": timeout_seconds,
                    }
                ),
            )

        apply_cmd = ["ast-grep", "run", "-p", args.pattern, "-r", rewrite]
        if args.lang:
            apply_cmd.extend(["--lang", args.lang])
        apply_cmd.extend(["-U", relative_path])
        completed = _run_ast_grep(cmd=apply_cmd, workspace=workspace, timeout_seconds=timeout_seconds)
        if isinstance(completed, ToolFailure):
            return ToolFailure(self.definition.name, error=completed.error)
        if not _is_no_match_result(completed):
            _raise_on_process_failure(completed=completed, fallback_message="ast-grep replace failed")

        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(f"Applied {replacement_count} AST replacement(s) in {relative_path}"),
            body=OpaqueToolBody(
                {
                    "path": relative_path,
                    "pattern": args.pattern,
                    "rewrite": args.rewrite,
                    "lang": args.lang,
                    "replacement_count": replacement_count,
                    "matches": matches,
                    "applied": True,
                    "mode": "replace",
                    "timeout_seconds": timeout_seconds,
                }
            ),
        )
