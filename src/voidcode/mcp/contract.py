"""
MCP Runtime Contract - Frozen Boundary Definitions

This module documents the current frozen MCP runtime contract surface.
These boundaries are considered stable and should not be changed without
careful consideration and versioning.

Last Updated: 2026-04-14
Issue: https://github.com/lei-jia-xing/voidcode/issues/107
"""

from __future__ import annotations

# ERROR CODES


class McpErrorCode:
    """Standard MCP error codes used by the runtime."""

    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_REQUEST = "invalid_request"


# CONTRACT BOUNDARY NOTES

# BOUNDARY NOTES:
#
# 1. Runtime Ownership: The runtime (src/voidcode/runtime/mcp.py) owns MCP server
#    session lifecycle through the official Python MCP SDK. VoidCode keeps the
#    product-specific policy layer while delegating protocol framing, initialize
#    negotiation, request/response correlation, and stdio process teardown to the
#    SDK client foundation.
#
# 2. Deferred Discovery: MCP servers are started lazily on first list_tools or
#    call_tool invocation, never hidden behind the cache. A run whose configured
#    servers are all covered by the persisted catalog (runtime/mcp_tool_cache.py)
#    connects to nothing; a cold install, or a server whose configuration
#    changed, discovers once at run start and persists the surface, so the first
#    turn still sees the configured MCP tools. Further connections happen only
#    when an MCP tool call or an explicit status/refresh request needs a server.
#
# 3. Tool Naming: MCP tools are exposed with the naming convention:
#    mcp/{server_name}/{tool_name}  # noqa: ERA001
#
# 4. Error Handling: All MCP errors are converted to ValueError with descriptive
#    messages. Runtime events and diagnostics are emitted for startup,
#    discovery/call, timeout, protocol, and shutdown failures.
#
# 5. Events: The runtime emits events for:
#    - runtime.mcp_server_started
#    - runtime.mcp_server_stopped
#
# 6. Protocol Version: Runtime initialize handshakes use the official Python SDK's
#    latest supported protocol version.
#
# 7. Tool Governance: MCP tool annotations are mapped into McpToolSafety. Tools
#    default to mutating unless the server explicitly marks them read-only and
#    non-destructive.
