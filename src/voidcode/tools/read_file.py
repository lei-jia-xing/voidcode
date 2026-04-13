"""Safe read-only file tool for the deterministic slice."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ._pydantic_args import ReadFileArgs
from .contracts import ToolCall, ToolDefinition, ToolResult


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

        relative_path = Path(args.path)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()

        if not candidate.is_relative_to(workspace_root):
            raise ValueError("read_file only allows paths inside the workspace")

        if not candidate.is_file():
            raise ValueError(f"read_file target does not exist: {args.path}")

        content = candidate.read_text(encoding="utf-8")
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data={
                "path": candidate.relative_to(workspace_root).as_posix(),
                "line_count": len(content.splitlines()),
            },
        )
