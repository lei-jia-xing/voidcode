from __future__ import annotations

import difflib
from pathlib import Path
from typing import ClassVar, final

from pydantic import ValidationError

from ..formatter import FormatterExecutor, formatter_diagnostics, formatter_payload
from ..hook.config import RuntimeHooksConfig
from ..security.path_policy import resolve_workspace_path
from ._pydantic_args import WriteFileArgs, format_validation_error
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
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc

        resolution = resolve_workspace_path(
            workspace=workspace,
            raw_path=args.path,
            containment_error="write_file only allows paths inside the workspace",
            allow_outside_workspace=True,
        )
        workspace_root = resolution.workspace_root
        candidate = resolution.candidate

        candidate.parent.mkdir(parents=True, exist_ok=True)
        old_content = candidate.read_text(encoding="utf-8") if candidate.exists() else ""
        candidate.write_text(args.content, encoding="utf-8")

        formatter_result = None
        if self._hooks_config is not None:
            formatter_result = FormatterExecutor(self._hooks_config, workspace_root).run(candidate)

        diagnostics = formatter_diagnostics(formatter_result)
        display_path = (
            str(candidate.resolve()) if resolution.is_external else resolution.relative_path
        )
        content = f"Wrote file successfully: {display_path}"
        if diagnostics:
            content += f" Formatter warning: {diagnostics[0]['message']}"

        new_content = candidate.read_text(encoding="utf-8")
        relative_output_path = (
            candidate.relative_to(workspace_root).as_posix()
            if not resolution.is_external
            else candidate.as_posix()
        )
        diff = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{relative_output_path}",
                tofile=f"b/{relative_output_path}",
            )
        )

        data: dict[str, object] = {
            "path": display_path,
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
