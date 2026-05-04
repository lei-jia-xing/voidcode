from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from ..formatter import (
    FormatterExecutionResult,
    FormatterExecutor,
    formatter_diagnostics,
    formatter_payload,
)
from ..hook.config import RuntimeHooksConfig
from ..security.path_policy import resolve_workspace_path
from ._pydantic_args import MultiEditArgs
from ._repair import ToolDiagnosticError
from .contracts import ToolCall, ToolDefinition, ToolResult
from .edit import (
    EditTool,
    read_utf8_text,
    summarize_diff,
)


class MultiEditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="multi_edit",
        description="Apply multiple edits to a file sequentially.",
        input_schema={
            "path": {"type": "string", "description": "Path to file"},
            "edits": {
                "type": "array",
                "description": "Array of {oldString, newString, replaceAll}",
            },
        },
        read_only=False,
    )

    def __init__(
        self,
        *,
        hooks_config: RuntimeHooksConfig | None = None,
        edit_tool: EditTool | None = None,
    ) -> None:
        self._hooks_config = hooks_config
        self._edit_tool = edit_tool or EditTool()

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        raw_path_value = call.arguments.get("path")
        if raw_path_value is None:
            raw_path_value = call.arguments.get("filePath")

        try:
            args = MultiEditArgs.model_validate(
                {
                    "path": raw_path_value,
                    "edits": call.arguments.get("edits", []),
                }
            )
        except ValidationError as exc:
            first_error = exc.errors()[0]
            location = first_error.get("loc", ())
            field_name = location[0] if location else None

            if field_name == "path":
                raise ValueError("multi_edit requires a string path argument") from exc
            if field_name == "edits" and first_error.get("type") == "value_error":
                raise ValueError("multi_edit requires at least one edit entry") from exc
            if field_name == "edits" and len(location) == 1:
                raise ValueError("multi_edit requires an array edits argument") from exc
            if len(location) >= 2 and location[0] == "edits" and len(location) == 2:
                idx = int(location[1]) + 1
                raise ValueError(f"multi_edit edit #{idx} must be an object") from exc
            if len(location) >= 3 and location[0] == "edits":
                idx = int(location[1]) + 1
                item_field = location[2]
                if item_field == "oldString":
                    raise ValueError(f"multi_edit edit #{idx} requires string oldString") from exc
                if item_field == "newString":
                    raise ValueError(f"multi_edit edit #{idx} requires string newString") from exc
                if item_field == "replaceAll":
                    raise ValueError(f"multi_edit edit #{idx} replaceAll must be boolean") from exc
            raise ValueError("multi_edit requires an array edits argument") from exc

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
        content_before = read_utf8_text(target)

        applied = 0
        details: list[dict[str, object]] = []
        for idx, item in enumerate(args.edits, start=1):
            try:
                result = self._edit_tool.invoke(
                    ToolCall(
                        tool_name="edit",
                        arguments={
                            "path": relative_target,
                            "oldString": item.oldString,
                            "newString": item.newString,
                            "replaceAll": item.replaceAll,
                        },
                    ),
                    workspace=workspace,
                )
            except ValueError as exc:
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
                        "Re-read the file after the successfully applied edits, then retry "
                        "multi_edit with only the remaining corrected edits."
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
            applied += 1
            details.append({"index": idx, "result": result.data})

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

        display_path = str(target.resolve()) if resolution.is_external else relative_target
        content = f"Applied {applied} edits to {display_path}"
        if diagnostics:
            content += f" Formatter warning: {diagnostics[0]['message']}"

        data: dict[str, object] = {
            "path": display_path,
            "applied": applied,
            "edits": details,
            "additions": additions,
            "deletions": deletions,
            "diff": diff,
        }
        if formatter_result is not None and formatter_result.status != "not_configured":
            data["formatter"] = formatter_payload(formatter_result)
        if diagnostics:
            data["diagnostics"] = diagnostics

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=data,
        )
