"""
MCP Runtime Contract - Frozen Boundary Definitions

This module documents the current frozen MCP runtime contract surface.
These boundaries are considered stable and should not be changed without
careful consideration and versioning.

Last Updated: 2026-04-14
Issue: https://github.com/lei-jia-xing/voidcode/issues/107
"""

from __future__ import annotations

# =============================================================================
# SUPPORTED CAPABILITIES (FROZEN)
# =============================================================================

SUPPORTED_CAPABILITIES = {
    # Transport
    "transport": ["stdio", "remote-http"],
    # Discovery
    "discovery": ["deferred"],  # Tool discovery happens on first list_tools call
    # Operations
    "operations": ["tools/list", "tools/call"],
    # Lifecycle
    "lifecycle": ["runtime-owned", "sdk-managed-client"],  # Runtime owns SDK client sessions
    # Security
    "security": ["trusted-local-only", "tool-annotation-governance"],
    # Observability
    "observability": ["runtime-events", "diagnostics-collector"],
}

# =============================================================================
# NOT SUPPORTED (EXPLICITLY EXCLUDED)
# =============================================================================

NOT_SUPPORTED = {
    "bidirectional_mcp": "Full bidirectional MCP not supported",
    "resources": "MCP resources not supported",
    "prompts": "MCP prompts not supported",
    "sampling": "MCP sampling not supported",
    "untrusted_servers": "Untrusted MCP servers not supported",
}

# =============================================================================
# CONTRACT VERSION
# =============================================================================

CONTRACT_VERSION = "1.0.0"
CONTRACT_VERSION_DATE = "2026-04-14"

# Protocol/version semantics are intentionally centralized here and re-exported
# through voidcode.mcp.types. Runtime implementations must not hardcode a
# divergent MCP protocol version for initialize handshakes.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25",)

# =============================================================================
# ERROR CODES
# =============================================================================


class McpErrorCode:
    """Standard MCP error codes used by the runtime."""

    SERVER_STARTUP_FAILED = "server_startup_failed"
    SERVER_NOT_FOUND = "server_not_found"
    REQUEST_TIMEOUT = "request_timeout"
    INVALID_JSON = "invalid_json"
    STDIO_CLOSED = "stdio_closed"
    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_REQUEST = "invalid_request"
    PROTOCOL_NEGOTIATION_FAILED = "protocol_negotiation_failed"
    REMOTE_CONNECTION_FAILED = "remote_connection_failed"
    REMOTE_HTTP_ERROR = "remote_http_error"


# =============================================================================
# DIAGNOSTIC TYPES
# =============================================================================

DIAGNOSTIC_CATEGORIES = {
    "startup": [
        "server_command_missing",
        "server_command_empty",
        "server_url_missing",
        "stdio_pipe_failed",
        "env_config_invalid",
        "remote_connection_failed",
    ],
    "communication": [
        "json_decode_failed",
        "response_id_mismatch",
        "unexpected_response_type",
        "protocol_negotiation_failed",
        "remote_http_error",
    ],
    "timeout": [
        "request_timeout",
        "server_unresponsive",
        "remote_timeout",
    ],
    "shutdown": [
        "graceful_shutdown_failed",
        "force_kill_required",
    ],
}

# =============================================================================
# CONTRACT BOUNDARY NOTES
# =============================================================================

"""
BOUNDARY NOTES:

1. Runtime Ownership: The runtime (src/voidcode/runtime/mcp.py) owns MCP server
   session lifecycle through the official Python MCP SDK. VoidCode keeps the
   product-specific policy layer while delegating protocol framing, initialize
   negotiation, request/response correlation, and stdio process teardown to the
   SDK client foundation.

2. Deferred Discovery: MCP servers are started lazily on first list_tools or
   call_tool invocation. There is no pre-initialization.

3. Tool Naming: MCP tools are exposed with the naming convention:
   mcp/{server_name}/{tool_name}

4. Error Handling: All MCP errors are converted to ValueError with descriptive
   messages. Runtime events and diagnostics are emitted for startup,
   discovery/call, timeout, protocol, and shutdown failures.

5. Events: The runtime emits events for:
   - runtime.mcp_server_started
   - runtime.mcp_server_stopped

6. Protocol Version: Runtime initialize handshakes use the official Python SDK's
   latest supported protocol version. The capability layer exposes supported
   protocol versions for contract review and test fixtures.

7. Tool Governance: MCP tool annotations are mapped into McpToolSafety. Tools
   default to mutating unless the server explicitly marks them read-only and
   non-destructive.
"""
