"""
VoidCode MCP Module

This module contains stable MCP configuration, types, contract definitions,
and observability interfaces. These are static data structures that do not
depend on runtime lifecycle.

Issue: https://github.com/lei-jia-xing/voidcode/issues/107
"""

from __future__ import annotations

# Config - static configuration models
from .config import (
    DEFAULT_MCP_TRANSPORT,
    McpConfig,
    McpServerConfig,
    McpTransport,
    create_mcp_config,
    create_mcp_server_config,
)

# Contract - frozen boundary definitions
from .contract import (
    CONTRACT_VERSION,
    CONTRACT_VERSION_DATE,
    DIAGNOSTIC_CATEGORIES,
    NOT_SUPPORTED,
    SUPPORTED_CAPABILITIES,
    McpErrorCode,
)

# Observability - diagnostics interfaces
from .observability import (
    McpDiagnostic,
    McpDiagnosticsCollector,
    McpDiagnosticSeverity,
    McpEventType,
    create_diagnostic,
    diagnostic_message,
)

# Types - static data structures
from .types import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    MCP_PROTOCOL_VERSION,
    McpConfigState,
    McpManager,
    McpManagerState,
    McpRuntimeEvent,
    McpToolCallResult,
    McpToolDescriptor,
)

__all__ = [
    # Types
    "McpConfigState",
    "McpManager",
    "McpManagerState",
    "McpRuntimeEvent",
    "McpToolCallResult",
    "McpToolDescriptor",
    "MCP_CLIENT_NAME",
    "MCP_CLIENT_VERSION",
    "MCP_PROTOCOL_VERSION",
    # Config
    "DEFAULT_MCP_TRANSPORT",
    "McpConfig",
    "McpServerConfig",
    "McpTransport",
    "create_mcp_config",
    "create_mcp_server_config",
    # Contract
    "CONTRACT_VERSION",
    "CONTRACT_VERSION_DATE",
    "DIAGNOSTIC_CATEGORIES",
    "McpErrorCode",
    "NOT_SUPPORTED",
    "SUPPORTED_CAPABILITIES",
    # Observability
    "McpDiagnostic",
    "McpDiagnosticSeverity",
    "McpDiagnosticsCollector",
    "McpEventType",
    "create_diagnostic",
    "diagnostic_message",
]

# Module version
__version__ = "1.0.0"
