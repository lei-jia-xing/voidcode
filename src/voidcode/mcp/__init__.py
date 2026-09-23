"""
VoidCode MCP Module

This module contains stable MCP types, contract definitions, and
observability interfaces. These are static data structures that do not
depend on runtime lifecycle.

Issue: https://github.com/lei-jia-xing/voidcode/issues/107
"""

from __future__ import annotations

# Builtin descriptors - static server definitions
from .builtin import (
    BuiltinMcpDescriptor,
    get_builtin_mcp_descriptor,
    list_builtin_mcp_descriptors,
)

# Contract - frozen boundary definitions
from .contract import McpErrorCode

# Observability - diagnostics interfaces
from .observability import (
    McpDiagnostic,
    McpDiagnosticsCollector,
    McpDiagnosticSeverity,
    create_diagnostic,
    diagnostic_message,
)
from .redaction import format_redacted_mcp_command, redact_mcp_command

# Types - static data structures
from .types import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    McpCachedToolSurface,
    McpConfigState,
    McpManager,
    McpManagerState,
    McpRuntimeEvent,
    McpServerRuntimeState,
    McpToolCallResult,
    McpToolDescriptor,
    McpToolSafety,
)

__all__ = [
    # Types
    "McpCachedToolSurface",
    "McpConfigState",
    "McpManager",
    "McpManagerState",
    "McpRuntimeEvent",
    "McpServerRuntimeState",
    "McpToolCallResult",
    "McpToolDescriptor",
    "McpToolSafety",
    "BuiltinMcpDescriptor",
    "get_builtin_mcp_descriptor",
    "list_builtin_mcp_descriptors",
    "MCP_CLIENT_NAME",
    "MCP_CLIENT_VERSION",
    # Contract
    "McpErrorCode",
    # Observability
    "McpDiagnostic",
    "McpDiagnosticSeverity",
    "McpDiagnosticsCollector",
    "create_diagnostic",
    "diagnostic_message",
    "format_redacted_mcp_command",
    "redact_mcp_command",
]

# Module version
__version__ = "1.0.0"
