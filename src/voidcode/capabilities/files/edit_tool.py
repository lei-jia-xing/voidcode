from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    ConfirmedStop,
    ResourceChange,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSemantic,
    ToolSuccess,
)

from ._replace import replace_file
from .apply_patch import execute_patch
from .errors import ToolDiagnosticError, raise_tool_diagnostic


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


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _convert_line_endings(text: str, ending: str) -> str:
    normalized = _normalize_line_endings(text)
    return normalized if ending == "\n" else normalized.replace("\n", "\r\n")


def read_utf8_text(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        raise ValueError("edit only supports UTF-8 text files") from exc


def summarize_diff(*, path: Path, before: str, after: str) -> tuple[str, int, int]:
    lines = list(
        difflib.unified_diff(
            StringIO(before, newline="").readlines(),
            StringIO(after, newline="").readlines(),
            fromfile=str(path),
            tofile=str(path),
        )
    )
    additions = sum(line.startswith("+") for line in lines[2:])
    deletions = sum(line.startswith("-") for line in lines[2:])
    diff = "".join(line if line.endswith(("\r", "\n")) else line + "\n\\ No newline at end of file\n" for line in lines)
    return diff, additions, deletions


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
    expectedHash: str = Field(description="SHA-256 of the current file bytes, as reported by read.")


class _BatchEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(min_length=1, pattern=r"\S")
    expectedHash: str = Field(description="SHA-256 of the current file bytes, as reported by read.")
    edits: list[_Replacement] = Field(min_length=1)


class _PatchEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    patch: str = Field(min_length=1, description="*** Begin Patch / *** End Patch text with Add File, Update File or Delete File operations.")
    expectedHashes: dict[str, str] | None = None


_EDIT_ARGS = TypeAdapter(_SingleEdit | _BatchEdit | _PatchEdit)


class EditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="edit",
        description=(
            "Edit files by replacing text (single replacement or ordered batch) or applying a patch. "
            "Replacements require exact current file text. Patches use *** Begin Patch marker format with add, update, delete and move operations. "
            "When constructing oldString from read output, omit any leading line-number prefix such as '42: ' and pass only the file's actual text."
        ),
        input_schema={"type": "object", **_EDIT_ARGS.json_schema()},
        effects=frozenset({ToolEffect.WRITE}),
        semantics=frozenset({ToolSemantic.RESOURCE_EDIT}),
        path_argument_keys=("path",),
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,  # noqa: ARG002 - required by the frozen Tool ABI
    ) -> ToolResult:
        args = _EDIT_ARGS.validate_python(json_wire_object(call.arguments))
        if isinstance(args, _PatchEdit):
            return execute_patch(args.model_dump(exclude_none=True), context=context, tool_name=self.definition.name)
        return self._invoke_replacements(args, context=context)

    def _invoke_replacements(self, args: _SingleEdit | _BatchEdit, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        path_value = args.path
        batch = isinstance(args, _BatchEdit)
        parsed_edits = args.edits if isinstance(args, _BatchEdit) else (args,)

        workspace_root = workspace.resolve()
        requested_path = Path(path_value)
        candidate = (requested_path if requested_path.is_absolute() else workspace_root / requested_path).resolve()
        is_external = not candidate.is_relative_to(workspace_root)
        if not candidate.exists():
            raise ValueError(f"edit target does not exist: {path_value}")
        if not candidate.is_file():
            raise ValueError(f"edit target is not a file: {path_value}")

        display_path = candidate.as_posix() if is_external else candidate.relative_to(workspace_root).as_posix()

        expected_hash = args.expectedHash
        content_before = read_utf8_text(candidate)
        actual_hash = hashlib.sha256(content_before.encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise_tool_diagnostic(
                message="Edit rejected because the file changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry the edit.",
                details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
            )

        ending = _detect_line_ending(content_before)

        # Preflight all edits against exact current text entirely in memory.
        current_content = content_before
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
                    item.oldString,
                    item.newString,
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

        final_text = _convert_line_endings(current_content, ending)
        final_bytes = final_text.encode("utf-8")
        final_disk_hash = hashlib.sha256(final_bytes).hexdigest()
        diff, additions, deletions = summarize_diff(
            path=candidate,
            before=content_before,
            after=final_text,
        )
        # Prepare the complete result before the single commit.
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return ToolFailure(
                self.definition.name,
                error="edit cancelled before committing file",
                execution=ConfirmedStop(cancellation_signalled=True),
            )
        replace_file(candidate, final_bytes, expected_content=content_before.encode("utf-8"))

        if batch:
            content = f"Applied {len(parsed_edits)} edits to {display_path}"
        else:
            content = "Edit applied successfully."
            if match_count > 1:
                content += f" ({match_count} occurrences replaced)"

        return ToolSuccess(
            self.definition.name,
            output=TextOutput(content),
            changes=(ResourceChange(display_path),),
            body=EditResultBody(
                path=display_path,
                additions=additions,
                deletions=deletions,
                match_count=total_matches,
                diff=diff,
                content_hash=final_disk_hash,
            ),
        )
