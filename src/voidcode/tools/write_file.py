from __future__ import annotations

import difflib
from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ..hook.config import RuntimeHooksConfig
from ._formatter import FormatterExecutor, formatter_diagnostics, formatter_payload
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

    def __init__(self, *, hooks_config: RuntimeHooksConfig | None = None) -> None:
        self._hooks_config = hooks_config

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
        old_content = candidate.read_text(encoding="utf-8") if candidate.exists() else ""
        candidate.write_text(args.content, encoding="utf-8")

        formatter_result = None
        if self._hooks_config is not None:
            formatter_result = FormatterExecutor(self._hooks_config, workspace_root).run(candidate)

        diagnostics = formatter_diagnostics(formatter_result)
        content = f"Wrote file successfully: {candidate.relative_to(workspace_root).as_posix()}"
        if diagnostics:
            content += f" Formatter warning: {diagnostics[0]['message']}"

        new_content = candidate.read_text(encoding="utf-8")
        relative_output_path = candidate.relative_to(workspace_root).as_posix()
        diff = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{relative_output_path}",
                tofile=f"b/{relative_output_path}",
            )
        )

        data: dict[str, object] = {
            "path": relative_output_path,
            "byte_count": candidate.stat().st_size,
            "diff": diff,
        }
        if formatter_result is not None and formatter_result.status != "not_configured":
            data["formatter"] = formatter_payload(formatter_result)
            data["byte_count"] = len(candidate.read_text(encoding="utf-8").encode("utf-8"))
        if diagnostics:
            data["diagnostics"] = diagnostics

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=data,
        )
