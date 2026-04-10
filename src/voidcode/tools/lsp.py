"""LSP Tool: read-only adapter over the runtime-managed LSP subsystem."""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any, ClassVar, Protocol, cast

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

        position = {"line": line_value - 1, "character": character_value - 1}
        text_document = {"uri": candidate.as_uri()}
        params: dict[str, object] = {"textDocument": text_document, "position": position}
        if operation == LspOperation.WORKSPACE_SYMBOL:
            params = {"query": ""}

        if operation in (LspOperation.INCOMING_CALLS, LspOperation.OUTGOING_CALLS):
            prepare_result = self._requester(
                server_name=server,
                method=LspOperation.PREPARE_CALL_HIERARCHY.value,
                params={"textDocument": text_document, "position": position},
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

    def __repr__(self) -> str:  # pragma: no cover
        return "<LspTool runtime-managed>"
