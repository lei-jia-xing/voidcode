from __future__ import annotations

import json
from pathlib import Path

from voidcode.mcp import McpToolDescriptor, McpToolSafety
from voidcode.runtime.config import RuntimeMcpServerConfig
from voidcode.runtime.mcp_tool_cache import (
    CACHE_VERSION,
    McpToolCatalogCache,
    mcp_server_identity,
)


def _descriptor(tool_name: str = "echo", *, enabled: bool = True) -> McpToolDescriptor:
    return McpToolDescriptor(
        server_name="echo",
        tool_name=tool_name,
        description=f"{tool_name} description",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        safety=McpToolSafety(read_only=True, destructive=False, idempotent=True, open_world=False, source="server-annotations"),
        enabled=enabled,
        disabled_reason=None if enabled else "invalid tool schema",
    )


def test_mcp_tool_catalog_cache_round_trips_tool_surface(tmp_path: Path) -> None:
    cache_path = tmp_path / "mcp-tool-catalog.json"
    identity = mcp_server_identity(
        "echo",
        RuntimeMcpServerConfig(transport="stdio", command=("python", "echo.py")),
        workspace_root=tmp_path,
    )

    McpToolCatalogCache(path=cache_path).store(server_name="echo", identity=identity, descriptors=(_descriptor(), _descriptor("shout")))
    restored = McpToolCatalogCache(path=cache_path).descriptors_for(server_name="echo", identity=identity)

    assert [descriptor.tool_name for descriptor in restored] == ["echo", "shout"]
    assert restored[0].server_name == "echo"
    assert restored[0].description == "echo description"
    assert restored[0].input_schema == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    assert restored[0].safety.read_only is True
    assert restored[0].safety.source == "server-annotations"
    assert restored[0].enabled is True


def test_mcp_tool_catalog_cache_preserves_disabled_descriptors(tmp_path: Path) -> None:
    cache_path = tmp_path / "mcp-tool-catalog.json"
    cache = McpToolCatalogCache(path=cache_path)

    cache.store(server_name="echo", identity="identity-a", descriptors=(_descriptor(enabled=False),))

    restored = McpToolCatalogCache(path=cache_path).descriptors_for(server_name="echo", identity="identity-a")
    assert restored[0].enabled is False
    assert restored[0].disabled_reason == "invalid tool schema"


def test_mcp_tool_catalog_cache_rejects_stale_identity_and_unknown_server(tmp_path: Path) -> None:
    cache = McpToolCatalogCache(path=tmp_path / "mcp-tool-catalog.json")
    cache.store(server_name="echo", identity="identity-a", descriptors=(_descriptor(),))

    assert cache.descriptors_for(server_name="echo", identity="identity-b") == ()
    assert cache.entry_for(server_name="echo", identity="identity-b") is None
    assert cache.descriptors_for(server_name="missing", identity="identity-a") == ()


def test_mcp_tool_catalog_cache_remembers_a_server_with_no_tools(tmp_path: Path) -> None:
    cache_path = tmp_path / "mcp-tool-catalog.json"
    McpToolCatalogCache(path=cache_path).store(server_name="empty", identity="identity-a", descriptors=())

    restored = McpToolCatalogCache(path=cache_path)

    # A known-empty surface is remembered (it is not the same as unknown).
    assert restored.entry_for(server_name="empty", identity="identity-a") == ()
    assert restored.entry_for(server_name="empty", identity="identity-b") is None


def test_mcp_tool_catalog_cache_store_keeps_other_servers(tmp_path: Path) -> None:
    cache = McpToolCatalogCache(path=tmp_path / "mcp-tool-catalog.json")
    cache.store(server_name="echo", identity="identity-a", descriptors=(_descriptor(),))
    cache.store(server_name="other", identity="identity-b", descriptors=(_descriptor("other-tool"),))

    restored_cache = McpToolCatalogCache(path=tmp_path / "mcp-tool-catalog.json")
    assert [descriptor.tool_name for descriptor in restored_cache.descriptors_for(server_name="echo", identity="identity-a")] == ["echo"]
    assert [descriptor.tool_name for descriptor in restored_cache.descriptors_for(server_name="other", identity="identity-b")] == ["other-tool"]


def test_mcp_tool_catalog_cache_ignores_unreadable_or_stale_payloads(tmp_path: Path) -> None:
    cache_path = tmp_path / "mcp-tool-catalog.json"
    cache_path.write_text("{ not json", encoding="utf-8")
    assert McpToolCatalogCache(path=cache_path).descriptors_for(server_name="echo", identity="identity-a") == ()

    cache_path.write_text(json.dumps({"version": CACHE_VERSION + 1, "servers": {"echo": {"identity": "a", "tools": []}}}), encoding="utf-8")
    assert McpToolCatalogCache(path=cache_path).descriptors_for(server_name="echo", identity="a") == ()

    cache_path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "servers": {
                    "broken": {"identity": 7, "tools": [{"name": "echo"}]},
                    "echo": {
                        "identity": "identity-a",
                        "tools": [
                            {"name": "ignored", "input_schema": "not-an-object"},
                            {"name": "kept", "input_schema": {"type": "object"}},
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    restored = McpToolCatalogCache(path=cache_path).descriptors_for(server_name="echo", identity="identity-a")
    assert [descriptor.tool_name for descriptor in restored] == ["kept"]
    assert restored[0].safety.read_only is False
    assert restored[0].safety.source == "default-deny"


def test_mcp_tool_catalog_cache_writes_nothing_when_persistence_fails(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    cache = McpToolCatalogCache(path=blocked_parent / "mcp-tool-catalog.json")

    cache.store(server_name="echo", identity="identity-a", descriptors=(_descriptor(),))

    assert cache.descriptors_for(server_name="echo", identity="identity-a")[0].tool_name == "echo"


def test_mcp_server_identity_tracks_configuration_and_workspace(tmp_path: Path) -> None:
    command_config = RuntimeMcpServerConfig(transport="stdio", command=("python", "echo.py"))
    url_config = RuntimeMcpServerConfig(transport="remote-http", url="https://example.test/mcp")

    assert mcp_server_identity("echo", command_config, workspace_root=tmp_path) == mcp_server_identity(
        "echo", command_config, workspace_root=tmp_path
    )
    assert mcp_server_identity("echo", command_config, workspace_root=tmp_path) != mcp_server_identity(
        "echo", command_config, workspace_root=tmp_path / "other"
    )
    assert mcp_server_identity("echo", command_config, workspace_root=tmp_path) != mcp_server_identity(
        "echo",
        RuntimeMcpServerConfig(transport="stdio", command=("python", "other.py")),
        workspace_root=tmp_path,
    )
    assert mcp_server_identity("echo", url_config, workspace_root=tmp_path) != mcp_server_identity(
        "echo",
        RuntimeMcpServerConfig(transport="remote-http", url="https://other.test/mcp"),
        workspace_root=tmp_path,
    )
    # Remote servers do not depend on the workspace they were discovered from.
    assert mcp_server_identity("echo", url_config, workspace_root=tmp_path) == mcp_server_identity(
        "echo", url_config, workspace_root=tmp_path / "other"
    )
