"""LSP Tool: basic client for Language Server Protocol queries.

This tool provides a minimal, read-only interface to an external LSP server
via a simple JSON-RPC over TCP/localhost. It supports a subset of common LSP
operations requested by the system:
- goToDefinition
- findReferences
- hover
- documentSymbol
- workspaceSymbol
- goToImplementation
- prepareCallHierarchy
- incomingCalls
- outgoingCalls

Note: This is intentionally lightweight. It performs a best-effort available
check and will return a helpful error message if no LSP server is configured or
reachable. The actual JSON-RPC framing follows the LSP Content-Length header
convention.
"""

from __future__ import annotations

import enum
import json
import os
import socket
from pathlib import Path
from typing import Any, ClassVar

from .contracts import ToolCall, ToolDefinition, ToolResult


@enum.unique
class LspOperation(enum.Enum):
    GO_TO_DEFINITION = "textDocument/definition"
    FIND_REFERENCES = "textDocument/references"
    HOVER = "textDocument/hover"
    DOCUMENT_SYMBOL = "textDocument/documentSymbol"
    WORKSPACE_SYMBOL = "workspace/symbol"
    GO_TO_IMPLEMENTATION = "textDocument/implementation"
    PREPARE_CALL_HIERARCHY = "textDocument/callHierarchy/prepareCallHierarchy"
    INCOMING_CALLS = "textDocument/callHierarchy/incomingCalls"
    OUTGOING_CALLS = "textDocument/callHierarchy/outgoingCalls"


class LspTool:
    """Tool to interact with an external LSP server.

    The tool validates that an LSP server is reachable via environment
    configuration and then issues a single JSON-RPC request corresponding to the
    selected operation.
    """

    # Tool contract definition
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="lsp",
        description="LSP client for basic code intelligence lookups.",
        input_schema={
            "operation": {"type": "string"},
            "filePath": {"type": "string"},
            "line": {"type": "integer"},
            "character": {"type": "integer"},
        },
        read_only=True,
    )

    def __init__(self) -> None:
        # Cached server address; resolved on first use
        self._host, self._port = self._resolve_server_address()

    # Public API expected by the runtime
    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        # Extract args
        op_value = call.arguments.get("operation")
        file_path = call.arguments.get("filePath")
        line = call.arguments.get("line")
        character = call.arguments.get("character")

        if not isinstance(file_path, str):
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error="lsp requires a string 'filePath' argument",
            )
        if not isinstance(line, int) or not isinstance(character, int):
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error="lsp requires integer 'line' and 'character' arguments (1-based)",
            )
        if op_value is None:
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error="lsp invocation requires 'operation' argument",
            )

        # Normalize operation
        try:
            if isinstance(op_value, LspOperation):
                operation = op_value
            else:
                operation = LspOperation(op_value)
        except Exception:
            # Be permissive: allow a string key like "GO_TO_DEFINITION" to map to enum
            if isinstance(op_value, str):
                try:
                    operation = LspOperation[op_value]
                except Exception:
                    return ToolResult(
                        tool_name=self.definition.name,
                        status="error",
                        error=f"Unsupported LSP operation: {op_value}",
                    )
            else:
                return ToolResult(
                    tool_name=self.definition.name,
                    status="error",
                    error=f"Unsupported LSP operation: {op_value}",
                )

        # Resolve file path and ensure it's inside workspace (mirror read_file.py)
        relative_path = Path(file_path)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error="lsp target must be inside the current workspace",
            )
        if not candidate.is_file():
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error=f"lsp target does not exist: {file_path}",
            )

        # Ensure server is available
        if not self._host or not self._port:
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error=(
                    "LSP server host/port not configured. Set VOIDCODE_LSP_HOST "
                    "and VOIDCODE_LSP_PORT."
                ),
            )
        if not self._server_is_available():
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error=f"LSP server not available at {self._host}:{self._port}",
            )

        # Prepare a minimal JSON-RPC payload for the requested operation
        position = {"line": max(0, int(line) - 1), "character": max(0, int(character) - 1)}
        textDocument = {"uri": f"file://{str(candidate)}"}
        params: dict[str, Any] = {"textDocument": textDocument, "position": position}
        if operation == LspOperation.WORKSPACE_SYMBOL:
            params = {"query": ""}
        if operation in (
            LspOperation.PREPARE_CALL_HIERARCHY,
            LspOperation.INCOMING_CALLS,
            LspOperation.OUTGOING_CALLS,
        ):
            # Minimal default for call hierarchy related requests
            params = {"textDocument": textDocument, "position": position}

        # Send the request
        response = self._send_request(operation.value, params)
        if response is None:
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error="No response from LSP server",
            )
        # If the LSP server returns an error-like payload, surface it
        if "error" in response or "code" in response:
            error_value = response.get("error")
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error=str(error_value) if isinstance(error_value, str) else str(response),
                data={"lsp_response": response},
            )

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            data={"lsp_response": response},
        )

    # Internal helpers
    @staticmethod
    def _resolve_server_address() -> tuple[str, int | None]:
        host = os.environ.get("VOIDCODE_LSP_HOST", "127.0.0.1")
        port = os.environ.get("VOIDCODE_LSP_PORT")
        if port is None:
            return host, None
        try:
            return host, int(port)
        except ValueError:
            return host, None

    def _server_is_available(self) -> bool:
        if not self._host or not self._port:
            return False
        try:
            with socket.create_connection((self._host, int(self._port)), timeout=0.5):
                return True
        except Exception:
            return False

    def _send_request(self, method: str, params: dict[str, object]) -> dict[str, object] | None:
        if not self._host or not self._port:
            return None
        # Prepare a lightweight JSON-RPC 2.0 request
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        body = payload.encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            with socket.create_connection((self._host, int(self._port)), timeout=3) as s:
                s.sendall(header + body)
                # Read response header
                resp_header = b""
                while b"\r\n\r\n" not in resp_header:
                    chunk = s.recv(1)
                    if not chunk:
                        break
                    resp_header += chunk
                # Parse Content-Length
                header_text = resp_header.decode("ascii", errors="ignore")
                cl = None
                for line in header_text.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        try:
                            cl = int(line.split(":", 1)[1].strip())
                        except Exception:
                            cl = None
                        break
                if cl is None:
                    return None
                # Read body
                body_bytes = b""
                remaining = cl
                while remaining > 0:
                    chunk = s.recv(remaining)
                    if not chunk:
                        break
                    body_bytes += chunk
                    remaining -= len(chunk)
                if not body_bytes:
                    return None
                return json.loads(body_bytes.decode("utf-8"))
        except Exception:
            return None

    # Expose a convenient alias for the interface elsewhere if needed
    def __repr__(self) -> str:  # pragma: no cover
        return f"<LspTool host={self._host} port={self._port}>"
