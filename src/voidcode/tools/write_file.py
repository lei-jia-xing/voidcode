from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ._pydantic_args import WriteFileArgs
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
        try:
            args = WriteFileArgs.model_validate(
                {
                    "path": call.arguments.get("path"),
                    "content": call.arguments.get("content"),
                }
            )
        except ValidationError as exc:
            first_error = exc.errors()[0]
            field_name = first_error.get("loc", (None,))[0]
            if field_name == "content":
                raise ValueError("write_file requires a string content argument") from exc
            raise ValueError("write_file requires a string path argument") from exc

        relative_path = Path(args.path)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()

        if not candidate.is_relative_to(workspace_root):
            raise ValueError("write_file only allows paths inside the workspace")

        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(args.content, encoding="utf-8")

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Wrote file successfully: {candidate.relative_to(workspace_root).as_posix()}",
            data={
                "path": candidate.relative_to(workspace_root).as_posix(),
                "byte_count": len(args.content.encode("utf-8")),
            },
        )
