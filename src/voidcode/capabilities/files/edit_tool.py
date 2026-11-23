from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from voidcode.sdk.atomic_file import replace_file
from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from ..tool_errors import ToolDiagnosticError, expected_failures, raise_tool_diagnostic
from ._common import (
    PathEscapesWorkspaceError,
    encode_line_endings,
    make_file_change,
    normalize_line_endings,
    resolve_workspace_path,
    summarize_diff,
)
from .mutation import check_expected_hash, invalid_path_failure, load_text_state, outside_workspace_failure, uniform_line_ending
from .range_edit import RangeEditResultBody, _RangeEditArgs, apply_range_edits


@dataclass(frozen=True, slots=True)
class EditResultBody:
    path: str
    additions: int
    deletions: int
    match_count: int
    diff: str
    content_hash: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "additions": self.additions,
            "deletions": self.deletions,
            "match_count": self.match_count,
            "diff": self.diff,
        }
        if self.content_hash is not None:
            payload["content_hash"] = self.content_hash
        return payload


def _replace(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
) -> tuple[str, int]:
    if old_string == new_string:
        raise_tool_diagnostic(
            message="No changes to apply: oldString and newString are identical.",
            error_kind="no_op",
            reason="identical_old_and_new",
            retry_guidance="Provide a newString that differs from oldString, or use read to confirm the file already has the desired content.",
        )
    if not content:
        raise_tool_diagnostic(
            message="Content is empty; nothing to edit.",
            error_kind="no_op",
            reason="empty_content",
            retry_guidance="The target file is empty; use write to create content, or read it again before retrying.",
        )

    match_count = content.count(old_string) if old_string else 0
    if match_count == 0:
        raise_tool_diagnostic(
            message="Exact oldString was not found in the current file text.",
            error_kind="tool_input_mismatch",
            reason="old_string_not_found",
            retry_guidance="Read the target path and retry with exact current file text, without line-number prefixes.",
        )
    if not replace_all and match_count > 1:
        raise_tool_diagnostic(
            message="Multiple exact matches found. Use replaceAll to replace every occurrence.",
            error_kind="ambiguous_match",
            reason="ambiguous_match",
            retry_guidance="Set replaceAll=true or provide longer exact text that uniquely identifies the intended location.",
            details={
                "match_count": match_count,
            },
        )

    return content.replace(old_string, new_string, -1 if replace_all else 1), match_count if replace_all else 1


class _Replacement(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    oldString: str
    newString: str
    replaceAll: bool = False


class _SingleEdit(_Replacement):
    path: str = Field(min_length=1, pattern=r"\S")
    expectedHash: str = Field(description="SHA-256 of the current file bytes, as reported by read or read(hash_only=true).")


class _BatchEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, pattern=r"\S")
    expectedHash: str = Field(description="SHA-256 of the current file bytes, as reported by read or read(hash_only=true).")
    edits: list[_Replacement] = Field(min_length=1)


_EDIT_ARGS = TypeAdapter(_SingleEdit | _BatchEdit | _RangeEditArgs)

_EDIT_DESCRIPTION = (
    "Edit files. Text mode replaces exact current file text (single replacement or ordered batch). "
    "Range mode (mode='range') applies 1-based line/character edits across one or more files, each checked "
    "against expectedHash and optionally an LSP document version. "
    "When constructing oldString from read output, omit any leading line-number prefix such as '42: ' and pass only the file's actual text. "
    "Line endings are matched EOL-insensitively; mixed-ending files are refused."
)


class EditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="edit",
        icon=Icon(glyph="✎"),
        description=_EDIT_DESCRIPTION,
        input_schema={"type": "object", **_EDIT_ARGS.json_schema()},
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path", "edits[].path"),
    )

    @expected_failures
    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        args = _EDIT_ARGS.validate_python(json_wire_object(call.arguments))
        if isinstance(args, _RangeEditArgs):
            return apply_range_edits(args, context=context, services=services)
        return self._invoke_replacements(args, context=context)

    def _invoke_replacements(self, args: _SingleEdit | _BatchEdit, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        path_value = args.path
        batch = isinstance(args, _BatchEdit)
        parsed_edits = args.edits if isinstance(args, _BatchEdit) else (args,)

        try:
            candidate, display_path = resolve_workspace_path(workspace, path_value, allow_outside_workspace=False)
        except PathEscapesWorkspaceError:
            return outside_workspace_failure(self.definition.name, path_value)
        except (OSError, ValueError) as exc:
            return invalid_path_failure(self.definition.name, path_value, detail=str(exc))
        if not candidate.exists():
            raise_tool_diagnostic(
                message=f"edit target does not exist: {path_value}",
                error_kind="tool_input_mismatch",
                reason="target_not_found",
                retry_guidance="Retry with an existing workspace file path.",
                details={"path": display_path},
            )
        if not candidate.is_file():
            raise_tool_diagnostic(
                message=f"edit target is not a file: {path_value}",
                error_kind="tool_input_mismatch",
                reason="not_a_regular_file",
                retry_guidance="Retry with a regular file path.",
                details={"path": display_path},
            )

        state = load_text_state(candidate, display_path, tool_name=self.definition.name)
        check_expected_hash(state, args.expectedHash, tool_name=self.definition.name)
        ending = uniform_line_ending(state.text, display_path=display_path, tool_name=self.definition.name)

        # Preflight all edits against LF-normalized current text entirely in memory.
        current_content = normalize_line_endings(state.text)
        total_matches = 0

        for idx, item in enumerate(parsed_edits, start=1):
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return ToolFailure(
                    self.definition.name,
                    error="edit cancelled before committing file",
                    execution=ConfirmedStop(cancellation_signalled=True),
                )
            try:
                next_content, match_count = _replace(
                    current_content,
                    normalize_line_endings(item.oldString),
                    normalize_line_endings(item.newString),
                    replace_all=item.replaceAll,
                )
            except ValueError as exc:
                if not batch:
                    raise
                message = (
                    f"edit failed at edit #{idx} of {len(parsed_edits)} for {display_path}.\n"
                    f"Prepared edits before failure: {idx - 1}; no file changes were written.\n"
                    "Retry guidance: correct the failing edit and retry the complete batch against the unchanged file.\n"
                    f"Underlying edit diagnostic:\n{exc}"
                )
                cause_details: dict[str, object] = {}
                if isinstance(exc, ToolDiagnosticError):
                    cause_details = {
                        "error_kind": exc.error_kind,
                        "error_details": exc.error_details,
                        "retry_guidance": exc.retry_guidance,
                    }
                raise ToolDiagnosticError(
                    message=message,
                    error_kind="tool_input_mismatch",
                    retry_guidance=("No batch edits were written. Correct the failing edit and retry the complete batch with the current file hash."),
                    error_details={
                        "reason": "edit_failed",
                        "path": display_path,
                        "failed_edit_index": idx,
                        "prepared_edits": idx - 1,
                        "applied_edits": 0,
                        "remaining_edits": len(parsed_edits) - idx + 1,
                        "cause": cause_details,
                    },
                ) from exc

            current_content = next_content
            total_matches += match_count

        final_text = encode_line_endings(current_content, ending)
        final_bytes = final_text.encode("utf-8")
        final_disk_hash = hashlib.sha256(final_bytes).hexdigest()
        diff, additions, deletions = summarize_diff(
            path=candidate,
            before=state.text,
            after=final_text,
        )
        # Prepare the complete result before the single commit.
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="edit cancelled before committing file",
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        replace_file(candidate, final_bytes, expected_content=state.raw)

        if batch:
            content = f"Applied {len(parsed_edits)} edits to {display_path}"
        else:
            content = "Edit applied successfully."
            if match_count > 1:
                content += f" ({match_count} occurrences replaced)"
        content += f"\nSHA-256 content hash: {final_disk_hash}"

        return ToolSuccess(
            self.definition.name,
            output=TextOutput(content),
            changes=(make_file_change(display_path),),
            body=EditResultBody(
                path=display_path,
                additions=additions,
                deletions=deletions,
                match_count=total_matches,
                diff=diff,
                content_hash=final_disk_hash,
            ),
        )


__all__ = ["EditResultBody", "EditTool", "RangeEditResultBody", "summarize_diff"]
