"""LSP Tool: read-only adapter over the runtime-managed LSP subsystem."""

from __future__ import annotations

import enum
import subprocess
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast

from lsprotocol import converters as lsp_converters
from lsprotocol import types as lsp_types

from ..hook.config import RuntimeFormatterPresetConfig
from .contracts import ToolCall, ToolDefinition, ToolResult


class LspRequester(Protocol):
    def __call__(
        self,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> Any: ...


@enum.unique
class LspOperation(enum.Enum):
    GO_TO_DEFINITION = "textDocument/definition"
    FIND_REFERENCES = "textDocument/references"
    HOVER = "textDocument/hover"
    DOCUMENT_SYMBOL = "textDocument/documentSymbol"
    WORKSPACE_SYMBOL = "workspace/symbol"
    GO_TO_IMPLEMENTATION = "textDocument/implementation"
    PREPARE_CALL_HIERARCHY = "textDocument/prepareCallHierarchy"
    INCOMING_CALLS = "callHierarchy/incomingCalls"
    OUTGOING_CALLS = "callHierarchy/outgoingCalls"


class LspTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="lsp",
        description="LSP client for basic code intelligence lookups.",
        input_schema={
            "operation": {"type": "string"},
            "filePath": {"type": "string"},
            "line": {"type": "integer"},
            "character": {"type": "integer"},
            "server": {"type": "string"},
        },
        read_only=True,
    )

    def __init__(self, *, requester: LspRequester) -> None:
        self._requester = requester
        self._converter = lsp_converters.get_converter()

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        op_value = call.arguments.get("operation")
        file_path = call.arguments.get("filePath")
        line = call.arguments.get("line")
        character = call.arguments.get("character")
        server = call.arguments.get("server")

        if server is not None and not isinstance(server, str):
            raise ValueError("lsp requires a string 'server' argument when provided")
        if not isinstance(file_path, str):
            raise ValueError("lsp requires a string 'filePath' argument")
        if isinstance(line, (int, float)):
            line_value = int(line)
        else:
            line_value = None

        if isinstance(character, (int, float)):
            character_value = int(character)
        else:
            character_value = None

        if line_value is None or character_value is None:
            raise ValueError("lsp requires numeric 'line' and 'character' arguments (1-based)")
        if line_value < 1 or character_value < 1:
            raise ValueError("lsp line and character must be >= 1")
        if op_value is None:
            raise ValueError("lsp invocation requires 'operation' argument")

        try:
            if isinstance(op_value, LspOperation):
                operation = op_value
            else:
                operation = LspOperation(op_value)
        except Exception:
            if isinstance(op_value, str):
                try:
                    operation = LspOperation[op_value]
                except Exception as exc:
                    raise ValueError(f"Unsupported LSP operation: {op_value}") from exc
            else:
                raise ValueError(f"Unsupported LSP operation: {op_value}") from None

        relative_path = Path(file_path)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            raise ValueError("lsp target must be inside the current workspace")
        if not candidate.is_file():
            raise ValueError(f"lsp target does not exist: {file_path}")

        position = lsp_types.Position(line=line_value - 1, character=character_value - 1)
        text_document = lsp_types.TextDocumentIdentifier(uri=candidate.as_uri())
        params: dict[str, object] = self._converter.unstructure(
            lsp_types.TextDocumentPositionParams(text_document=text_document, position=position),
            unstructure_as=lsp_types.TextDocumentPositionParams,
        )
        if operation == LspOperation.WORKSPACE_SYMBOL:
            params = self._converter.unstructure(
                lsp_types.WorkspaceSymbolParams(query=""),
                unstructure_as=lsp_types.WorkspaceSymbolParams,
            )

        if operation in (LspOperation.INCOMING_CALLS, LspOperation.OUTGOING_CALLS):
            prepare_result = self._requester(
                server_name=server,
                method=LspOperation.PREPARE_CALL_HIERARCHY.value,
                params=self._converter.unstructure(
                    lsp_types.TextDocumentPositionParams(
                        text_document=text_document,
                        position=position,
                    ),
                    unstructure_as=lsp_types.TextDocumentPositionParams,
                ),
                workspace=workspace_root,
            )
            prepare_response = prepare_result.response
            prepare_error = prepare_response.get("error")
            if prepare_error is not None:
                raise ValueError(f"LSP prepareCallHierarchy error: {prepare_error}")

            prepare_payload = prepare_response.get("result")
            item: dict[str, object] | None = None
            if isinstance(prepare_payload, list) and prepare_payload:
                first = cast(object, prepare_payload[0])
                if isinstance(first, dict):
                    item = cast(dict[str, object], first)
            elif isinstance(prepare_payload, dict):
                item = cast(dict[str, object], prepare_payload)
            if item is None:
                raise ValueError("LSP prepareCallHierarchy returned no item")
            params = {"item": item}

            response = self._requester(
                server_name=server,
                method=operation.value,
                params=params,
                workspace=workspace_root,
            )
            error_value = response.response.get("error")
            if error_value is not None:
                raise ValueError(f"LSP error: {error_value}")
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                data={"lsp_response": response.response},
            )

        response = self._requester(
            server_name=server,
            method=operation.value,
            params=params,
            workspace=workspace_root,
        )
        error_value = response.response.get("error")
        if error_value is not None:
            raise ValueError(f"LSP error: {error_value}")
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            data={"lsp_response": response.response},
        )

    def __repr__(self) -> str:
        return "<LspTool runtime-managed>"


# ------------------------------------------------------------------------------
# FormatTool 内嵌在这里
# ------------------------------------------------------------------------------
FORMAT_DEFINITION = ToolDefinition(
    name="format_file",
    description=(
        "Auto-format a file using built-in formatter presets with default file mappings, "
        "project-root detection, and fallback commands."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to format"
            }
        },
        "required": ["path"],
    },
    read_only=False,
)


class FormatTool:
    def __init__(self, hooks_config, workspace: Path):
        self._hooks = hooks_config
        self._workspace = workspace.resolve()

    @property
    def definition(self) -> ToolDefinition:
        return FORMAT_DEFINITION

    def invoke(self, call: ToolCall, workspace: Path) -> ToolResult:
        file_path = self._resolve_target_path(call)
        resolved = self._hooks.resolve_formatter(file_path)

        if not resolved:
                return ToolResult(
                    tool_name=FORMAT_DEFINITION.name,
                    status="error",
                    error=f"No formatter available for {file_path}",
                    data={"path": str(file_path)},
                )

        lang, preset = resolved
        cwd = self._resolve_formatter_cwd(file_path=file_path, preset=preset)
        attempted_commands = [
            list(preset.command),
            *[list(cmd) for cmd in preset.fallback_commands],
        ]
        missing_tools: list[str] = []
        failed_attempts: list[tuple[list[str], subprocess.CompletedProcess[str]]] = []

        for command_parts in attempted_commands:
            cmd = [*command_parts, str(file_path)]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                missing_tools.append(command_parts[0])
                continue

            if proc.returncode == 0:
                return ToolResult(
                    tool_name=FORMAT_DEFINITION.name,
                    status="ok",
                    content=f"Successfully formatted {file_path.name} ({lang})",
                    data={
                        "path": str(file_path),
                        "language": lang,
                        "command": cmd,
                        "cwd": str(cwd),
                    },
                )

            failed_attempts.append((cmd, proc))

        if failed_attempts:
            last_cmd, last_proc = failed_attempts[-1]
            stderr = (last_proc.stderr or last_proc.stdout)[:300].strip()
            return ToolResult(
                tool_name=FORMAT_DEFINITION.name,
                status="error",
                error=(
                    f"Format failed for {file_path.name} using preset '{lang}' from {cwd}: "
                    f"{stderr or 'formatter exited with a non-zero status'}"
                ),
                data={
                    "path": str(file_path),
                    "language": lang,
                    "cwd": str(cwd),
                    "command": last_cmd,
                    "attempted_commands": [cmd + [str(file_path)] for cmd in attempted_commands],
                    "stdout": last_proc.stdout,
                    "stderr": last_proc.stderr,
                },
            )

        attempted_tool_names = ", ".join(dict.fromkeys(missing_tools))
        return ToolResult(
            tool_name=FORMAT_DEFINITION.name,
            status="error",
            error=(
                f"No formatter executable was available for preset '{lang}'. "
                f"Tried: {attempted_tool_names}. Install one of them or override "
                f"hooks.formatter_presets.{lang}.command in .voidcode.json."
            ),
            data={
                "path": str(file_path),
                "language": lang,
                "cwd": str(cwd),
                "attempted_commands": [cmd + [str(file_path)] for cmd in attempted_commands],
            },
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

    def _resolve_formatter_cwd(
        self, *, file_path: Path, preset: RuntimeFormatterPresetConfig
    ) -> Path:
        if preset.cwd_policy == "workspace":
            return self._workspace
        if preset.cwd_policy == "file_directory":
            return file_path.parent

        return self._find_nearest_root(file_path=file_path, preset=preset) or self._workspace

    def _find_nearest_root(
        self, *, file_path: Path, preset: RuntimeFormatterPresetConfig
    ) -> Path | None:
        if not preset.root_markers:
            return None

        current = file_path.parent
        while current.is_relative_to(self._workspace):
            if any((current / marker).exists() for marker in preset.root_markers):
                return current
            if current == self._workspace:
                break
            current = current.parent
        return None
