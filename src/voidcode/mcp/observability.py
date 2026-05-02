"""
MCP Observability - Diagnostics and Event Interfaces

This module provides interfaces and utilities for MCP runtime observability,
including diagnostic information and event logging.

Last Updated: 2026-04-14
Issue: https://github.com/lei-jia-xing/voidcode/issues/107
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class McpEventType(StrEnum):
    """MCP runtime event types."""

    SERVER_STARTED = "runtime.mcp_server_started"
    SERVER_STOPPED = "runtime.mcp_server_stopped"
    TOOL_LIST_START = "mcp.tool_list_start"
    TOOL_LIST_COMPLETE = "mcp.tool_list_complete"
    TOOL_CALL_START = "mcp.tool_call_start"
    TOOL_CALL_COMPLETE = "mcp.tool_call_complete"
    ERROR = "mcp.error"


class McpDiagnosticSeverity(StrEnum):
    """Diagnostic severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class McpDiagnostic:
    """Represents an MCP diagnostic event."""

    severity: McpDiagnosticSeverity
    category: str
    message: str
    server_name: str | None = None
    tool_name: str | None = None
    details: dict[str, Any] | None = None


class McpDiagnosticsCollector(Protocol):
    """Protocol for collecting MCP diagnostics."""

    def record_diagnostic(self, diagnostic: McpDiagnostic) -> None: ...
    def get_diagnostics(self) -> list[McpDiagnostic]: ...


class InMemoryMcpDiagnosticsCollector:
    """Simple diagnostics collector suitable for runtime state and tests."""

    def __init__(self) -> None:
        self._diagnostics: list[McpDiagnostic] = []

    def record_diagnostic(self, diagnostic: McpDiagnostic) -> None:
        self._diagnostics.append(diagnostic)

    def get_diagnostics(self) -> list[McpDiagnostic]:
        return list(self._diagnostics)


# Standard diagnostic messages


def diagnostic_message(
    code: str,
    *,
    server_name: str | None = None,
    tool_name: str | None = None,
    **details: Any,
) -> str:
    """Generate a standardized diagnostic message."""
    messages: dict[str, str] = {
        "server_startup_failed": "MCP server failed to start",
        "server_command_missing": "MCP server command is missing",
        "server_url_missing": "MCP server URL is missing for remote-http transport",
        "stdio_pipe_failed": "MCP server stdio pipe failed",
        "json_decode_failed": "MCP server returned invalid JSON",
        "request_timeout": "MCP server request timed out",
        "server_not_found": "MCP server not found in configuration",
        "tool_not_found": "MCP tool not found",
        "unexpected_response": "MCP server returned unexpected response",
        "remote_connection_failed": "Failed to connect to remote MCP server",
        "remote_http_error": "Remote MCP server returned HTTP error",
        "remote_timeout": "Remote MCP server request timed out",
    }
    base = messages.get(code, f"MCP[{code}]")
    if server_name:
        base = f"{base} (server: {server_name})"
    if tool_name:
        base = f"{base} (tool: {tool_name})"
    if details:
        detail_str = ", ".join(f"{k}={v}" for k, v in details.items())
        base = f"{base} [{detail_str}]"
    return base


def create_diagnostic(
    severity: McpDiagnosticSeverity,
    category: str,
    code: str,
    *,
    server_name: str | None = None,
    tool_name: str | None = None,
    **details: Any,
) -> McpDiagnostic:
    """Factory function to create a diagnostic with standardized message."""
    message = diagnostic_message(code, server_name=server_name, tool_name=tool_name, **details)

    return McpDiagnostic(
        severity=severity,
        category=category,
        message=message,
        server_name=server_name,
        tool_name=tool_name,
        details=details if details else None,
    )
