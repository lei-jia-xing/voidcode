"""LSP Tool: read-only adapter over the runtime-managed LSP subsystem."""

from __future__ import annotations

import enum
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast

from lsprotocol import converters as lsp_converters
from lsprotocol import types as lsp_types

from ..formatter import FormatterExecutor
from ..hook.config import RuntimeHooksConfig
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


_LSP_OPERATION_ALIASES: dict[str, LspOperation] = {
    "gotodefinition": LspOperation.GO_TO_DEFINITION,
    "definition": LspOperation.GO_TO_DEFINITION,
    "findreferences": LspOperation.FIND_REFERENCES,
    "references": LspOperation.FIND_REFERENCES,
    "hover": LspOperation.HOVER,
    "documentsymbol": LspOperation.DOCUMENT_SYMBOL,
    "symbol": LspOperation.DOCUMENT_SYMBOL,
    "workspacesymbol": LspOperation.WORKSPACE_SYMBOL,
    "gotoimplementation": LspOperation.GO_TO_IMPLEMENTATION,
    "implementation": LspOperation.GO_TO_IMPLEMENTATION,
    "incomingcalls": LspOperation.INCOMING_CALLS,
    "outgoingcalls": LspOperation.OUTGOING_CALLS,
}


class LspTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="lsp",
        description="LSP client for basic code intelligence lookups.",
        input_schema={
            "operation": {
                "type": "string",
                "description": (
                    "LSP operation. Preferred names include goToDefinition, "
                    "findReferences, hover, documentSymbol, workspaceSymbol, "
                    "goToImplementation, incomingCalls, and outgoingCalls. "
                    "Protocol method strings like textDocument/definition "
                    "also work."
                ),
            },
            "filePath": {
                "type": "string",
                "description": (
                    "Workspace-relative file path used for target selection and server resolution."
                ),
            },
            "line": {
                "type": "integer",
                "description": (
                    "1-based line number as shown in editors. Required for "
                    "position-based operations."
                ),
            },
            "character": {
                "type": "integer",
                "description": (
                    "1-based character number as shown in editors. Required "
                    "for position-based operations."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Optional workspaceSymbol search query. Empty string requests all symbols."
                ),
            },
            "server": {"type": "string"},
        },
        read_only=True,
    )

    def __init__(self, *, requester: LspRequester) -> None:
        self._requester = requester
        self._converter = lsp_converters.get_converter()

    @staticmethod
    def _parse_operation(value: object) -> LspOperation:
        if isinstance(value, LspOperation):
            return value
        if not isinstance(value, str):
            raise ValueError(f"Unsupported LSP operation: {value}")
        try:
            return LspOperation(value)
        except Exception:
            pass
        try:
            return LspOperation[value]
        except Exception:
            pass
        normalized = "".join(character for character in value if character.isalnum()).lower()
        alias = _LSP_OPERATION_ALIASES.get(normalized)
        if alias is not None:
            return alias
        raise ValueError(f"Unsupported LSP operation: {value}")

    @staticmethod
    def _invoke_requester(
        requester: LspRequester,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> Any:
        try:
            return requester(
                server_name=server_name,
                method=method,
                params=params,
                workspace=workspace,
            )
        except ValueError as exc:
            runtime_lsp = import_module("voidcode.runtime.lsp")
            runtime_error = getattr(runtime_lsp, "LspRuntimeError", None)
            if runtime_error is not None and isinstance(exc, runtime_error):
                raise ValueError(f"LSP protocol error: {exc}") from exc
            raise

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        op_value = call.arguments.get("operation")
        file_path = call.arguments.get("filePath")
        line = call.arguments.get("line")
        character = call.arguments.get("character")
        query = call.arguments.get("query")
        server = call.arguments.get("server")

        if server is not None and not isinstance(server, str):
            raise ValueError("lsp requires a string 'server' argument when provided")
        if query is not None and not isinstance(query, str):
            raise ValueError("lsp requires a string 'query' argument when provided")
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

        if op_value is None:
            raise ValueError("lsp invocation requires 'operation' argument")

        operation = self._parse_operation(op_value)
        position_required = operation not in (
            LspOperation.DOCUMENT_SYMBOL,
            LspOperation.WORKSPACE_SYMBOL,
        )
        if position_required and (line_value is None or character_value is None):
            raise ValueError("lsp requires numeric 'line' and 'character' arguments (1-based)")
        if line_value is not None and line_value < 1:
            raise ValueError("lsp line and character must be >= 1")
        if character_value is not None and character_value < 1:
            raise ValueError("lsp line and character must be >= 1")

        relative_path = Path(file_path)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            raise ValueError("lsp target must be inside the current workspace")
        if not candidate.is_file():
            raise ValueError(f"lsp target does not exist: {file_path}")

        position = (
            lsp_types.Position(line=line_value - 1, character=character_value - 1)
            if line_value is not None and character_value is not None
            else None
        )
        text_document = lsp_types.TextDocumentIdentifier(uri=candidate.as_uri())
        params: dict[str, object]
        if operation == LspOperation.DOCUMENT_SYMBOL:
            params = cast(
                dict[str, object],
                self._converter.unstructure(
                    lsp_types.DocumentSymbolParams(text_document=text_document),
                    unstructure_as=lsp_types.DocumentSymbolParams,
                ),
            )
        elif position is not None:
            params = cast(
                dict[str, object],
                self._converter.unstructure(
                    lsp_types.TextDocumentPositionParams(
                        text_document=text_document, position=position
                    ),
                    unstructure_as=lsp_types.TextDocumentPositionParams,
                ),
            )
        else:
            params = cast(dict[str, object], {"textDocument": {"uri": candidate.as_uri()}})
        if operation == LspOperation.WORKSPACE_SYMBOL:
            params = cast(
                dict[str, object],
                self._converter.unstructure(
                    lsp_types.WorkspaceSymbolParams(query=query or ""),
                    unstructure_as=lsp_types.WorkspaceSymbolParams,
                ),
            )

        if operation in (LspOperation.INCOMING_CALLS, LspOperation.OUTGOING_CALLS):
            assert position is not None
            prepare_result = self._invoke_requester(
                self._requester,
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
            params = cast(dict[str, object], {"item": item})

            response = self._invoke_requester(
                self._requester,
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

        response = self._invoke_requester(
            self._requester,
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
