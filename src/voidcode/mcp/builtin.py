from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type BuiltinMcpTransport = Literal["stdio", "remote-http"]


@dataclass(frozen=True, slots=True)
class BuiltinMcpDescriptor:
    name: str
    transport: BuiltinMcpTransport
    description: str
    lifecycle: str
    url: str | None = None
    command: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


_BUILTIN_MCP_DESCRIPTORS: dict[str, BuiltinMcpDescriptor] = {
    "context7": BuiltinMcpDescriptor(
        name="context7",
        transport="remote-http",
        url="https://mcp.context7.com/mcp",
        lifecycle="descriptor_only_config_gated",
        description="Context7 documentation lookup MCP descriptor.",
        tags=("documentation", "research"),
    ),
    "websearch": BuiltinMcpDescriptor(
        name="websearch",
        transport="remote-http",
        url="https://mcp.exa.ai/mcp",
        lifecycle="descriptor_only_config_gated",
        description="Public web search MCP descriptor.",
        tags=("search", "research"),
    ),
    "grep_app": BuiltinMcpDescriptor(
        name="grep_app",
        transport="remote-http",
        url="https://mcp.grep.app",
        lifecycle="descriptor_only_config_gated",
        description=(
            "Code search MCP via grep.app remote endpoint. Loaded by default with the builtin remote MCP set; mcp.enabled=false disables it."
        ),
        tags=("code-search", "research"),
    ),
}


def list_builtin_mcp_descriptors() -> tuple[BuiltinMcpDescriptor, ...]:
    return tuple(_BUILTIN_MCP_DESCRIPTORS.values())


def get_builtin_mcp_descriptor(name: str) -> BuiltinMcpDescriptor | None:
    return _BUILTIN_MCP_DESCRIPTORS.get(name)


__all__ = [
    "BuiltinMcpDescriptor",
    "get_builtin_mcp_descriptor",
    "list_builtin_mcp_descriptors",
]
