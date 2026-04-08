from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

from .contracts import ToolCall, ToolDefinition, ToolResult


@final
class WriteFileTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="write_file",
        description="Write a UTF-8 text file inside the current workspace.",
        input_schema={"path": {"type": "string"}, "content": {"type": "string"}},
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        path_value = call.arguments.get("path")
        if not isinstance(path_value, str):
            raise ValueError("write_file requires a string path argument")

        content_value = call.arguments.get("content")
        if not isinstance(content_value, str):
            raise ValueError("write_file requires a string content argument")

        relative_path = Path(path_value)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()

        if not candidate.is_relative_to(workspace_root):
            raise ValueError("write_file only allows paths inside the workspace")

        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content_value, encoding="utf-8")

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content_value,
            data={
                "path": candidate.relative_to(workspace_root).as_posix(),
                "byte_count": len(content_value.encode("utf-8")),
            },
        )
