"""The read-only ast_grep search/preview tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from voidcode.capabilities.tool_errors import expected_failures, raise_tool_diagnostic
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import OpaqueToolBody, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess

from .args import AstGrepArgs
from .language import (
    _directory_globs,
    _file_matches_filters,
    _filter_matches,
    _ignore_flags,
    _native_jsonl,
    _resolved_language,
)
from .process import _extract_matches, _run_ast_grep, _truncated_or_failed, _warning_lines
from .rendering import _match_result_output, _search_budget, _warnings_block, _with_match_counts
from .targets import _resolve_search_candidate


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
                _extract_matches(completed, stage="search"),
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
                _extract_matches(completed, stage="preview"),
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


__all__ = ["AstGrepTool"]
