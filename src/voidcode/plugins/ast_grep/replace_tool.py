"""The workspace-only, single-file ast_grep replace tool."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, ClassVar

from voidcode.capabilities.files._common import MAX_MUTATION_CONTENT_BYTES
from voidcode.capabilities.tool_errors import expected_failures, raise_tool_diagnostic
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import PluginServices, ProcessResult
from voidcode.sdk.tools import (
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

from .args import AstGrepReplaceArgs
from .language import _ignore_flags, _resolved_language
from .process import (
    _APPLY_CAPTURE_BYTES,
    _extract_matches,
    _parse_applied_count,
    _run_ast_grep,
    _short,
    _spill_output,
    _truncated_or_failed,
    _warning_lines,
)
from .rendering import _match_result_output, _select_matches_for_budget, _warnings_block, _with_match_counts
from .targets import _file_digest, _load_replace_target, _raise_stale_edit, _ReplaceTarget, _resolve_replace_candidate


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
        matches = _extract_matches(completed, stage="replace preview")
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
        if applied.cancelled or applied.timed_out or applied.truncated or applied.exit_code != 0:
            return self._apply_failure(
                applied,
                target=target,
                display_path=display_path,
                services=services,
                timeout_seconds=timeout_seconds,
            )
        final = _load_replace_target(candidate, display_path)
        if final.source_hash == target.source_hash:
            message = f"ast-grep reported success but {display_path} is unchanged."
            message += _warnings_block(apply_warnings)
            return ToolFailure(
                tool_name=self.definition.name,
                error=message,
                output=TextOutput(message),
                diagnostics=ToolDiagnostics(
                    kind="no_op",
                    summary=message,
                    details={"path": display_path, "source_hash": target.source_hash, "warnings": list(apply_warnings)},
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


__all__ = ["AstGrepReplaceTool"]
