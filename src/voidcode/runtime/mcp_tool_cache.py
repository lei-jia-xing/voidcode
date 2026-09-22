"""Persisted catalog of MCP tools the runtime has already discovered.

The runtime never connects to an MCP server just to learn its tool surface.
Discovery happens when something needs it (an MCP-backed tool call, or an
explicit status/refresh request); the descriptors discovered then are written
here so later runs can advertise the same tool surface without reconnecting.

Stored entries are keyed by the *server identity* (transport plus command or
URL, and the stdio working directory), so a reconfigured server never inherits
another server's tool surface.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from ..mcp import McpToolDescriptor, McpToolSafety
from .config import RuntimeMcpServerConfig

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


def mcp_server_identity(
    server_name: str,
    config: RuntimeMcpServerConfig,
    *,
    workspace_root: Path | None,
) -> str:
    """Fingerprint the configuration a cached tool surface belongs to."""
    payload: dict[str, object] = {
        "server": server_name,
        "transport": config.transport,
        "scope": config.scope,
        "command": list(config.command),
        "url": config.url,
    }
    if config.transport == "stdio" and workspace_root is not None:
        # stdio servers run with the workspace as their working directory, so
        # the same command can describe different servers per workspace.
        payload["cwd"] = str(workspace_root)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _descriptor_payload(descriptor: McpToolDescriptor) -> dict[str, object]:
    return {
        "name": descriptor.tool_name,
        "description": descriptor.description,
        "input_schema": descriptor.input_schema,
        "enabled": descriptor.enabled,
        "disabled_reason": descriptor.disabled_reason,
        "safety": {
            "read_only": descriptor.safety.read_only,
            "destructive": descriptor.safety.destructive,
            "idempotent": descriptor.safety.idempotent,
            "open_world": descriptor.safety.open_world,
            "source": descriptor.safety.source,
        },
    }


def _optional_bool(payload: Mapping[str, object], key: str) -> bool | None:
    value = payload.get(key)
    return value if isinstance(value, bool) else None


def _descriptor_from_payload(server_name: str, payload: object) -> McpToolDescriptor | None:
    if not isinstance(payload, dict):
        return None
    entry = cast(dict[str, object], payload)
    tool_name = entry.get("name")
    input_schema = entry.get("input_schema")
    if not isinstance(tool_name, str) or not tool_name or not isinstance(input_schema, dict):
        return None
    raw_safety = entry.get("safety")
    safety_payload = cast(dict[str, object], raw_safety) if isinstance(raw_safety, dict) else {}
    return McpToolDescriptor(
        server_name=server_name,
        tool_name=tool_name,
        description=cast(str, entry.get("description")) if isinstance(entry.get("description"), str) else "",
        input_schema=cast(dict[str, object], input_schema),
        safety=McpToolSafety(
            read_only=safety_payload.get("read_only") is True,
            destructive=_optional_bool(safety_payload, "destructive"),
            idempotent=_optional_bool(safety_payload, "idempotent"),
            open_world=_optional_bool(safety_payload, "open_world"),
            source=(cast(str, safety_payload["source"]) if isinstance(safety_payload.get("source"), str) else "default-deny"),
        ),
        enabled=entry.get("enabled") is not False,
        disabled_reason=cast(str, entry["disabled_reason"]) if isinstance(entry.get("disabled_reason"), str) else None,
    )


@dataclass(slots=True)
class McpToolCatalogCache:
    """Read and write the discovered MCP tool catalog.

    All failures are non-fatal: a missing, stale, or corrupt cache only means
    the runtime advertises less until the next successful discovery.
    """

    path: Path
    _entries: dict[str, tuple[str, tuple[McpToolDescriptor, ...]]] | None = None

    def entry_for(self, *, server_name: str, identity: str) -> tuple[McpToolDescriptor, ...] | None:
        """Return the cached surface for a server, or ``None`` when unknown/stale."""
        entry = self._load().get(server_name)
        if entry is None or entry[0] != identity:
            return None
        return entry[1]

    def store(
        self,
        *,
        server_name: str,
        identity: str,
        descriptors: tuple[McpToolDescriptor, ...],
    ) -> None:
        """Record the discovered tool surface for one server."""
        entries = dict(self._load())
        entries[server_name] = (identity, descriptors)
        self._entries = entries
        self._persist()

    def _load(self) -> dict[str, tuple[str, tuple[McpToolDescriptor, ...]]]:
        if self._entries is not None:
            return self._entries
        self._entries = self._read()
        return self._entries

    def _read(self) -> dict[str, tuple[str, tuple[McpToolDescriptor, ...]]]:
        try:
            raw_payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError, UnicodeDecodeError:
            return {}
        if not isinstance(raw_payload, dict):
            return {}
        payload = cast(dict[str, object], raw_payload)
        if payload.get("version") != CACHE_VERSION:
            return {}
        raw_servers = payload.get("servers")
        if not isinstance(raw_servers, dict):
            return {}

        entries: dict[str, tuple[str, tuple[McpToolDescriptor, ...]]] = {}
        for raw_name, raw_entry in cast(dict[object, object], raw_servers).items():
            if not isinstance(raw_name, str) or not raw_name or not isinstance(raw_entry, dict):
                continue
            entry = cast(dict[str, object], raw_entry)
            identity = entry.get("identity")
            raw_tools = entry.get("tools")
            if not isinstance(identity, str) or not identity or not isinstance(raw_tools, list):
                continue
            descriptors = tuple(
                descriptor
                for descriptor in (_descriptor_from_payload(raw_name, tool) for tool in cast(list[object], raw_tools))
                if descriptor is not None
            )
            entries[raw_name] = (identity, descriptors)
        return entries

    def _persist(self) -> None:
        entries = self._entries or {}
        payload = {
            "version": CACHE_VERSION,
            "servers": {
                server_name: {
                    "identity": identity,
                    "tools": [_descriptor_payload(descriptor) for descriptor in descriptors],
                }
                for server_name, (identity, descriptors) in sorted(entries.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError:
            logger.debug("failed to persist MCP tool catalog cache", exc_info=True)


__all__ = ["CACHE_VERSION", "McpToolCatalogCache", "mcp_server_identity"]
