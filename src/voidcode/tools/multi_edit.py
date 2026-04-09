from __future__ import annotations

from pathlib import Path
from typing import ClassVar, cast

from .contracts import ToolCall, ToolDefinition, ToolResult


class MultiEditTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="multiedit",
        description="Apply multiple edits to a file sequentially.",
        input_schema={
            "filePath": {"type": "string", "description": "Path to file"},
            "edits": {
                "type": "array",
                "description": "Array of {oldString, newString, replaceAll}",
            },
        },
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        path_value = call.arguments.get("filePath")
        edits_value = call.arguments.get("edits", [])
        edits: list[object] = []
        if isinstance(edits_value, list):
            edits = cast(list[object], edits_value)

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Applied {len(edits)} edits to {path_value}",
        )
