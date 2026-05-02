from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BuiltinMcpDescriptor:
    name: str
    transport: str
    description: str
    lifecycle: str
    url: str | None = None
    command: tuple[str, ...] = ()
    scope: str = "runtime"
    skill_scoped: bool = False
    skill_name: str | None = None
    tags: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "transport": self.transport,
            "description": self.description,
            "lifecycle": self.lifecycle,
            "scope": self.scope,
            "skill_scoped": self.skill_scoped,
            "tags": list(self.tags),
        }
        if self.url is not None:
            payload["url"] = self.url
        if self.command:
            payload["command"] = list(self.command)
        if self.skill_name is not None:
            payload["skill_name"] = self.skill_name
        return payload


@dataclass(frozen=True, slots=True)
class BuiltinMcpDescriptorRegistry:
    descriptors: Mapping[str, BuiltinMcpDescriptor] = field(default_factory=dict)

    def get(self, name: str) -> BuiltinMcpDescriptor | None:
        return self.descriptors.get(name)

    def list_descriptors(self) -> tuple[BuiltinMcpDescriptor, ...]:
        return tuple(self.descriptors.values())


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
        transport="configured-server-intent",
        lifecycle="descriptor_only_config_gated",
        description=(
            "Optional code search MCP intent; execution requires a "
            "user-configured server named grep_app."
        ),
        tags=("code-search", "research"),
    ),
    "playwright": BuiltinMcpDescriptor(
        name="playwright",
        transport="stdio",
        command=("npx", "@playwright/mcp@latest"),
        lifecycle="skill_scoped_descriptor_only_config_gated",
        description=(
            "Playwright browser automation MCP descriptor scoped to the builtin playwright skill."
        ),
        scope="session",
        skill_scoped=True,
        skill_name="playwright",
        tags=("browser", "verification", "frontend"),
    ),
}


def list_builtin_mcp_descriptors() -> tuple[BuiltinMcpDescriptor, ...]:
    return tuple(_BUILTIN_MCP_DESCRIPTORS.values())


def get_builtin_mcp_descriptor(name: str) -> BuiltinMcpDescriptor | None:
    return _BUILTIN_MCP_DESCRIPTORS.get(name)


def load_builtin_mcp_descriptor_registry() -> BuiltinMcpDescriptorRegistry:
    return BuiltinMcpDescriptorRegistry(descriptors=dict(_BUILTIN_MCP_DESCRIPTORS))


def builtin_mcp_descriptor_names() -> tuple[str, ...]:
    return tuple(_BUILTIN_MCP_DESCRIPTORS)


def known_mcp_server_names(configured_server_names: Iterable[str] = ()) -> tuple[str, ...]:
    merged = dict.fromkeys((*_BUILTIN_MCP_DESCRIPTORS.keys(), *tuple(configured_server_names)))
    return tuple(merged)


__all__ = [
    "BuiltinMcpDescriptor",
    "BuiltinMcpDescriptorRegistry",
    "builtin_mcp_descriptor_names",
    "get_builtin_mcp_descriptor",
    "known_mcp_server_names",
    "list_builtin_mcp_descriptors",
    "load_builtin_mcp_descriptor_registry",
]
