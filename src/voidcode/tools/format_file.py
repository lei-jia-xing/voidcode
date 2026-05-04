"""Format file tool backed by runtime formatter presets."""

from __future__ import annotations

from pathlib import Path

from ..formatter import FormatterExecutor
from ..hook.config import RuntimeHooksConfig
from .contracts import ToolCall, ToolDefinition, ToolResult

FORMAT_DEFINITION = ToolDefinition(
    name="format_file",
    description=(
        "Auto-format a file using built-in formatter presets with default file mappings, "
        "project-root detection, and fallback commands."
    ),
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the file to format"}},
        "required": ["path"],
    },
    read_only=False,
)


class FormatTool:
    def __init__(self, hooks_config: RuntimeHooksConfig, workspace: Path) -> None:
        self._executor = FormatterExecutor(hooks_config, workspace)
        self._workspace = workspace.resolve()

    @property
    def definition(self) -> ToolDefinition:
        return FORMAT_DEFINITION

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        file_path = self._resolve_target_path(call)
        result = self._executor.run(file_path)

        if result.status == "not_configured":
            return ToolResult(
                tool_name=FORMAT_DEFINITION.name,
                status="error",
                error=f"No formatter available for {file_path}",
                data={"path": str(file_path)},
            )

        data: dict[str, object] = {"path": str(file_path)}
        if result.language is not None:
            data["language"] = result.language
        if result.cwd is not None:
            data["cwd"] = str(result.cwd)
        if result.command is not None:
            data["command"] = list(result.command)
        if result.attempted_commands:
            data["attempted_commands"] = [list(cmd) for cmd in result.attempted_commands]
        if result.stdout is not None:
            data["stdout"] = result.stdout
        if result.stderr is not None:
            data["stderr"] = result.stderr

        if result.status == "formatted":
            return ToolResult(
                tool_name=FORMAT_DEFINITION.name,
                status="ok",
                content=f"Successfully formatted {file_path.name} ({result.language})",
                data=data,
            )

        return ToolResult(
            tool_name=FORMAT_DEFINITION.name,
            status="error",
            error=result.error,
            data=data,
        )

    def _resolve_target_path(self, call: ToolCall) -> Path:
        raw_path = call.arguments.get("path")
        if not isinstance(raw_path, str):
            raise ValueError("format_file requires a string 'path' argument")

        file_path = (self._workspace / raw_path).resolve()
        if not file_path.is_relative_to(self._workspace):
            raise ValueError("format_file target must stay inside the current workspace")
        if not file_path.is_file():
            raise ValueError(f"format_file target does not exist: {raw_path}")
        return file_path
