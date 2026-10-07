from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, field_validator

from ..core.tool_context import ToolContext
from ..formatter import (
    FormatterExecutionResult,
    FormatterExecutor,
    formatter_diagnostics,
    formatter_payload,
)
from ..hook.config import RuntimeHooksConfig
from ..security.json_values import json_wire_object, own_json_object
from ..security.path_policy import resolve_workspace_path
from ._post_edit_diagnostics import post_edit_lsp_diagnostics
from ._pydantic_args import parse_tool_args, validate_non_empty
from ._repair import ToolDiagnosticError, raise_tool_diagnostic
from .contracts import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolSuccess
from .edit import EditResultBody, EditTool, read_utf8_text, summarize_diff
from .guards import enforce_read_before_write


class MultiEditItemArgs(BaseModel):
    oldString: str
    newString: str
    replaceAll: bool = False


class MultiEditArgs(BaseModel):
    path: str
    edits: list[MultiEditItemArgs]

    _validate_path = field_validator("path", mode="after")(validate_non_empty)

    @field_validator("edits", mode="after")
    @classmethod
    def _validate_edits(cls, value: list[MultiEditItemArgs]) -> list[MultiEditItemArgs]:
        if not value:
            raise ValueError("edits must contain at least one edit")
        return value


@dataclass(frozen=True, slots=True)
class MultiEditResultBody:
    path: str
    applied: int
    edits: tuple[tuple[int, EditResultBody], ...]
    additions: int
    deletions: int
    diff: str
    formatter: Mapping[str, object] | None = None
    diagnostics: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.formatter is not None:
            object.__setattr__(self, "formatter", own_json_object(self.formatter))
        object.__setattr__(self, "diagnostics", tuple(own_json_object(item) for item in self.diagnostics))

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "applied": self.applied,
            "edits": [{"index": index, "result": result.as_payload()} for index, result in self.edits],
            "additions": self.additions,
            "deletions": self.deletions,
            "diff": self.diff,
        }
        if self.formatter is not None:
            payload["formatter"] = json_wire_object(self.formatter)
        if self.diagnostics:
            payload["diagnostics"] = [json_wire_object(item) for item in self.diagnostics]
        return payload


class MultiEditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="multi_edit",
        description="Apply multiple edits to a file sequentially.",
        input_schema={
            "path": {"type": "string", "description": "Path to file"},
            "expectedHash": {
                "type": "string",
                "description": (
                    "Required SHA-256 hash of the current file content, taken from the read output's "
                    "SHA-256 content hash. Rejects stale edits when the file changed since that read."
                ),
            },
            "edits": {
                "type": "array",
                "minItems": 1,
                "description": "Ordered replacements; each oldString must match the current file state at that step.",
                "items": {
                    "type": "object",
                    "required": ["oldString", "newString"],
                    "properties": {
                        "oldString": {"type": "string", "description": "Exact text to replace."},
                        "newString": {"type": "string", "description": "Replacement text."},
                        "replaceAll": {"type": "boolean", "description": "Replace every match; defaults to false."},
                    },
                },
            },
            "required": ["path", "edits", "expectedHash"],
        },
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("path",),
    )

    def __init__(
        self,
        *,
        hooks_config: RuntimeHooksConfig | None = None,
        edit_tool: EditTool | None = None,
    ) -> None:
        self._hooks_config = hooks_config
        self._edit_tool = edit_tool or EditTool()

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolSuccess[MultiEditResultBody]:
        workspace = context.require_workspace()
        raw_path_value = call.arguments.get("path")

        args = parse_tool_args(
            MultiEditArgs,
            {
                "path": raw_path_value,
                "edits": call.arguments.get("edits", []),
            },
            tool_name=self.definition.name,
        )

        resolution = resolve_workspace_path(
            workspace=workspace,
            raw_path=args.path,
            containment_error="multi_edit only allows paths inside the workspace",
            allow_outside_workspace=True,
        )
        workspace_root = resolution.workspace_root
        target = resolution.candidate
        if not target.exists() or not target.is_file():
            raise ValueError(f"multi_edit target does not exist: {args.path}")

        relative_target = resolution.relative_path
        display_path = str(target.resolve()) if resolution.is_external else relative_target

        enforce_read_before_write(
            context=context,
            tool_name=self.definition.name,
            workspace=workspace_root,
            raw_path=args.path,
            candidate=target,
            display_path=display_path,
            is_external=resolution.is_external,
        )

        expected_hash = call.arguments.get("expectedHash")
        if not isinstance(expected_hash, str):
            raise_tool_diagnostic(
                message="multi_edit requires an expectedHash argument: the file must be read before it is edited.",
                error_kind="tool_input_mismatch",
                reason="missing_expected_hash",
                retry_guidance=(
                    "Use read on the target path, copy its SHA-256 content hash from the output, then retry multi_edit with that expectedHash."
                ),
                details={"path": display_path, "raw_path": args.path},
            )

        content_before = read_utf8_text(target)
        actual_hash = hashlib.sha256(content_before.encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise_tool_diagnostic(
                message="multi_edit rejected because the file changed since it was read (stale edit).",
                error_kind="stale_edit",
                reason="content_hash_mismatch",
                retry_guidance="Read the file again, use the SHA-256 content hash shown in read output, then retry multi_edit.",
                details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
            )

        applied = 0
        details: list[tuple[int, EditResultBody]] = []
        for idx, item in enumerate(args.edits, start=1):
            try:
                current_content = read_utf8_text(target)
                current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
                result = self._edit_tool.invoke(
                    ToolCall(
                        tool_name="edit",
                        arguments={
                            "path": relative_target,
                            "oldString": item.oldString,
                            "newString": item.newString,
                            "replaceAll": item.replaceAll,
                            "expectedHash": current_hash,
                        },
                    ),
                    context=context,
                )
            except ValueError as exc:
                if isinstance(exc, ToolDiagnosticError) and exc.error_details.get("reason") == "unseen_range":
                    raise exc
                message = (
                    "multi_edit failed at edit "
                    f"#{idx} of {len(args.edits)} for {relative_target}.\n"
                    f"Applied edits before failure: {applied}.\n"
                    "Retry guidance: re-read the file, keep the successful earlier edits in mind, "
                    "and retry from this failing edit with current file text.\n"
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
                    retry_guidance=(
                        "Re-read the file after the successfully applied edits, then retry multi_edit with only the remaining corrected edits."
                    ),
                    error_details={
                        "reason": "edit_failed",
                        "path": relative_target,
                        "failed_edit_index": idx,
                        "applied_edits": applied,
                        "remaining_edits": len(args.edits) - idx + 1,
                        "cause": cause_details,
                    },
                ) from exc
            assert isinstance(result.body, EditResultBody)
            applied += 1
            details.append((idx, result.body))

        formatter_result: FormatterExecutionResult | None = None
        if self._hooks_config is not None:
            formatter_result = FormatterExecutor(self._hooks_config, workspace_root).run(target)
        final_content = read_utf8_text(target)
        diff, additions, deletions = summarize_diff(
            path=target,
            before=content_before,
            after=final_content,
        )
        diagnostics = formatter_diagnostics(formatter_result)

        content = f"Applied {applied} edits to {display_path}"
        if diagnostics:
            content += f" Formatter warning: {diagnostics[0]['message']}"

        formatter = None
        if formatter_result is not None and formatter_result.status != "not_configured":
            formatter = formatter_payload(formatter_result)
        lsp_diagnostics = post_edit_lsp_diagnostics(
            context=context,
            workspace=workspace_root,
            paths=[display_path],
        )
        return ToolSuccess(
            self.definition.name,
            output=TextOutput(content),
            body=MultiEditResultBody(
                path=display_path,
                applied=applied,
                edits=tuple(details),
                additions=additions,
                deletions=deletions,
                diff=diff,
                formatter=formatter,
                diagnostics=tuple([*diagnostics, *lsp_diagnostics]),
            ),
        )
