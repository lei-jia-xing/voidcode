"""Safe read-only file tool for the deterministic slice."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ._pydantic_args import ReadFileArgs
from .contracts import ToolCall, ToolDefinition, ToolResult
from .workspace import resolve_workspace_path


@final
class ReadFileTool:
    """Read a UTF-8 text file from the current workspace."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="read_file",
        description="Read a UTF-8 text file inside the current workspace.",
        input_schema={"path": {"type": "string"}},
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        try:
            args = ReadFileArgs.model_validate({"path": call.arguments.get("path")})
        except ValidationError as exc:
            raise ValueError("read_file requires a string path argument") from exc

        candidate, relative_path = resolve_workspace_path(
            workspace=workspace,
            path_text=args.path,
            tool_name=self.definition.name,
            must_be_file=True,
        )

        content = candidate.read_text(encoding="utf-8")
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data={
                "path": relative_path,
                "line_count": len(content.splitlines()),
            },
        )
