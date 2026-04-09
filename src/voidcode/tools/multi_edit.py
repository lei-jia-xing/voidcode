from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, cast

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
        path_value = call.arguments.get("path")
        if path_value is None:
            path_value = call.arguments.get("filePath")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("multi_edit requires a string path argument")

        workspace_root = workspace.resolve()
        target = (workspace_root / Path(path_value)).resolve()
        if not target.is_relative_to(workspace_root):
            raise ValueError("multi_edit only allows paths inside the workspace")
        if not target.exists() or not target.is_file():
            raise ValueError(f"multi_edit target does not exist: {path_value}")

        relative_target = str(target.relative_to(workspace_root))
        edits_value = call.arguments.get("edits", [])
        edits: list[object] = []
        if isinstance(edits_value, list):
            edits = cast(list[object], edits_value)
        else:
            raise ValueError("multi_edit requires an array edits argument")

        if not edits:
            raise ValueError("multi_edit requires at least one edit entry")

        edit_tool = EditTool()
        applied = 0
        details: list[dict[str, object]] = []
        for idx, item in enumerate(edits, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"multi_edit edit #{idx} must be an object")
            item_dict = cast(dict[str, Any], item)

            old_string = item_dict.get("oldString")
            new_string = item_dict.get("newString")
            replace_all = item_dict.get("replaceAll", False)

            if not isinstance(old_string, str):
                raise ValueError(f"multi_edit edit #{idx} requires string oldString")
            if not isinstance(new_string, str):
                raise ValueError(f"multi_edit edit #{idx} requires string newString")
            if not isinstance(replace_all, bool):
                raise ValueError(f"multi_edit edit #{idx} replaceAll must be boolean")

            result = edit_tool.invoke(
                ToolCall(
                    tool_name="edit",
                    arguments={
                        "path": relative_target,
                        "oldString": old_string,
                        "newString": new_string,
                        "replaceAll": replace_all,
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
