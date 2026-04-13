from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from ._pydantic_args import MultiEditArgs
from .contracts import ToolCall, ToolDefinition, ToolResult
from .edit import EditTool


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

        workspace_root = workspace.resolve()
        target = (workspace_root / Path(args.path)).resolve()
        if not target.is_relative_to(workspace_root):
            raise ValueError("multi_edit only allows paths inside the workspace")
        if not target.exists() or not target.is_file():
            raise ValueError(f"multi_edit target does not exist: {args.path}")

        relative_target = target.relative_to(workspace_root).as_posix()

        edit_tool = EditTool()
        applied = 0
        details: list[dict[str, object]] = []
        for idx, item in enumerate(args.edits, start=1):
            result = edit_tool.invoke(
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
            applied += 1
            details.append({"index": idx, "result": result.data})

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Applied {applied} edits to {relative_target}",
            data={"path": relative_target, "applied": applied, "edits": details},
        )
