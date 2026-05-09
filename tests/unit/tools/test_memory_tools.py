from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.config import RuntimeAgentConfig, RuntimeConfig, RuntimeToolsConfig
from voidcode.runtime.memory import MemoryConfig
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore
from voidcode.runtime.tool_provider import BuiltinToolProvider, scoped_tool_registry_for_agent
from voidcode.tools import ToolCall, ToolResult
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context

MEMORY_TOOL_NAMES = ("memory_add", "memory_search", "memory_list", "memory_delete")
MEMORY_TOOL_EXPORTS = ("MemoryAddTool", "MemorySearchTool", "MemoryListTool", "MemoryDeleteTool")


def _arguments_by_tool() -> dict[str, dict[str, object]]:
    return {
        "memory_add": {"content": "remember me"},
        "memory_search": {"query": "remember"},
        "memory_list": {},
        "memory_delete": {"id": "mem_123"},
    }


def _read_result_items(result: ToolResult, key: str) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], result.data[key])


def _memory_tool_class(export_name: str) -> type[Any]:
    tools_module = __import__("voidcode.tools", fromlist=[export_name])
    return cast(type[Any], getattr(tools_module, export_name))


def _memory_tools() -> dict[str, Any]:
    return {
        "memory_add": _memory_tool_class("MemoryAddTool")(),
        "memory_search": _memory_tool_class("MemorySearchTool")(),
        "memory_list": _memory_tool_class("MemoryListTool")(),
        "memory_delete": _memory_tool_class("MemoryDeleteTool")(),
    }


def _memory_store(workspace: Path) -> SqliteSessionStore:
    return SqliteSessionStore(database_path=workspace.parent / "runtime-memory-tools.sqlite3")


def _memory_runtime(workspace: Path, *, enabled: bool = True) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=workspace,
        config=RuntimeConfig(memory=MemoryConfig(enabled=enabled)),
        session_store=_memory_store(workspace),
    )


def _invoke(
    tool: Any,
    arguments: dict[str, object],
    workspace: Path,
    *,
    memory: VoidCodeRuntime | None = None,
) -> ToolResult:
    with bind_runtime_tool_context(
        RuntimeToolInvocationContext(
            session_id="test-session",
            memory=memory or _memory_runtime(workspace),
        )
    ):
        return cast(
            ToolResult,
            tool.invoke(
                ToolCall(tool_name=tool.definition.name, arguments=arguments), workspace=workspace
            ),
        )


def _add_memory(
    *,
    workspace: Path,
    content: object = "ship runtime-owned memory",
    kind: str = "project",
    tags: list[str] | None = None,
) -> dict[str, object]:
    result = _invoke(
        _memory_tools()["memory_add"],
        {"content": content, "kind": kind, "tags": tags or []},
        workspace,
    )
    assert result.status == "ok"
    return result.data


def test_memory_tools_are_exported_and_registered_by_default() -> None:
    exported_names = __import__("voidcode.tools", fromlist=["__all__"]).__all__
    registry = ToolRegistry.with_defaults()

    for export_name, tool_name in zip(MEMORY_TOOL_EXPORTS, MEMORY_TOOL_NAMES, strict=True):
        assert export_name in exported_names
        assert registry.resolve(tool_name).definition.name == tool_name


def test_builtin_provider_exposes_memory_tools_for_registry_visibility() -> None:
    provided = {tool.definition.name: tool for tool in BuiltinToolProvider().provide_tools()}

    assert set(MEMORY_TOOL_NAMES).issubset(provided)


def test_memory_tool_definitions_encode_contract_and_mutability() -> None:
    registry = ToolRegistry.with_defaults()

    assert registry.resolve("memory_add").definition.read_only is False
    assert registry.resolve("memory_delete").definition.read_only is False
    assert registry.resolve("memory_search").definition.read_only is True
    assert registry.resolve("memory_list").definition.read_only is True

    add_schema = registry.resolve("memory_add").definition.input_schema
    search_schema = registry.resolve("memory_search").definition.input_schema
    list_schema = registry.resolve("memory_list").definition.input_schema
    delete_schema = registry.resolve("memory_delete").definition.input_schema

    assert set(add_schema.keys()) == {"content", "kind", "tags"}
    assert set(search_schema.keys()) == {"query", "limit", "kind", "tags"}
    assert set(list_schema.keys()) == {"limit", "kind", "tags"}
    assert set(delete_schema.keys()) == {"id"}

    for schema in (add_schema, search_schema, list_schema, delete_schema):
        assert "workspace" not in schema
        assert "path" not in schema
        assert "workspace_path" not in schema


def test_memory_tools_require_runtime_provided_memory_facade(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="runtime-provided memory facade"):
        _memory_tools()["memory_add"].invoke(
            ToolCall(tool_name="memory_add", arguments={"content": "needs runtime store"}),
            workspace=tmp_path,
        )


def test_memory_tools_source_does_not_instantiate_sqlite_store_directly() -> None:
    source = Path("src/voidcode/tools/memory.py").read_text(encoding="utf-8")

    assert "SqliteSessionStore" not in source
    assert "current_runtime_tool_context" in source


def test_memory_tools_respect_disabled_runtime_memory_policy(tmp_path: Path) -> None:
    runtime = _memory_runtime(tmp_path, enabled=False)

    with pytest.raises(RuntimeError, match="memory is disabled"):
        _invoke(
            _memory_tools()["memory_add"],
            {"content": "must not bypass runtime policy"},
            tmp_path,
            memory=runtime,
        )

    assert runtime.memory_status().total_count == 0


def test_memory_registry_allowlist_filters_memory_tools() -> None:
    registry = ToolRegistry.with_defaults()

    assert set(registry.filtered(("memory_*",)).tools) == set(MEMORY_TOOL_NAMES)

    scoped = scoped_tool_registry_for_agent(
        registry,
        agent=RuntimeAgentConfig(
            preset="leader",
            tools=RuntimeToolsConfig(allowlist=("memory_search", "memory_list")),
        ),
    )
    assert set(scoped.tools) == {"memory_search", "memory_list"}


def test_memory_tools_reject_arbitrary_workspace_or_path_arguments(tmp_path: Path) -> None:
    for tool_name, tool in _memory_tools().items():
        required_arguments = _arguments_by_tool()[tool_name]

        for forbidden_argument in ("workspace", "workspace_path", "path"):
            with pytest.raises(ValueError, match=forbidden_argument):
                _invoke(tool, {**required_arguments, forbidden_argument: str(tmp_path)}, tmp_path)


@pytest.mark.parametrize("content", ["", "   ", None])
def test_memory_add_rejects_empty_content(tmp_path: Path, content: object) -> None:
    with pytest.raises(ValueError, match="content"):
        _invoke(_memory_tools()["memory_add"], {"content": content}, tmp_path)


@pytest.mark.parametrize("tool_name", ["memory_add", "memory_search", "memory_list"])
def test_memory_tools_reject_invalid_kind(tmp_path: Path, tool_name: str) -> None:
    base_arguments = {
        "memory_add": {"content": "valid"},
        "memory_search": {"query": "valid"},
        "memory_list": {},
    }[tool_name]

    with pytest.raises(ValueError, match="kind"):
        _invoke(_memory_tools()[tool_name], {**base_arguments, "kind": "global"}, tmp_path)


@pytest.mark.parametrize("tool_name", ["memory_add", "memory_search", "memory_list"])
@pytest.mark.parametrize("tags", [[""], ["project", "project"], [123], "project"])
def test_memory_tools_reject_invalid_tags(tmp_path: Path, tool_name: str, tags: object) -> None:
    base_arguments = {
        "memory_add": {"content": "valid"},
        "memory_search": {"query": "valid"},
        "memory_list": {},
    }[tool_name]

    with pytest.raises(ValueError, match="tags"):
        _invoke(_memory_tools()[tool_name], {**base_arguments, "tags": tags}, tmp_path)


def test_memory_add_uses_project_kind_and_empty_tags_by_default(tmp_path: Path) -> None:
    result = _invoke(_memory_tools()["memory_add"], {"content": "default metadata"}, tmp_path)

    assert result.status == "ok"
    assert result.data["kind"] == "project"
    assert result.data["tags"] == []
    assert isinstance(result.data["id"], str)


def test_memory_add_accepts_json_compatible_structured_payloads(tmp_path: Path) -> None:
    payload = {"decision": "store runtime-owned memories", "links": ["ADR-001"]}

    data = _add_memory(workspace=tmp_path, content=payload, tags=["architecture"])

    json.dumps(data)
    assert data["content"] == payload
    assert data["tags"] == ["architecture"]


def test_memory_search_finds_current_workspace_memory_and_honors_limit_kind_tags(
    tmp_path: Path,
) -> None:
    _add_memory(
        workspace=tmp_path, content="alpha runtime memory", kind="project", tags=["runtime"]
    )
    _add_memory(
        workspace=tmp_path, content="alpha feedback memory", kind="feedback", tags=["runtime"]
    )
    _add_memory(workspace=tmp_path, content="beta runtime memory", kind="project", tags=["other"])

    result = _invoke(
        _memory_tools()["memory_search"],
        {"query": "alpha", "limit": 1, "kind": "project", "tags": ["runtime"]},
        tmp_path,
    )

    assert result.status == "ok"
    assert result.data["count"] == 1
    results = _read_result_items(result, "results")
    assert results == [
        {
            "id": results[0]["id"],
            "content": "alpha runtime memory",
            "kind": "project",
            "tags": ["runtime"],
        }
    ]


def test_memory_list_defaults_to_recent_project_memories_and_honors_filters(tmp_path: Path) -> None:
    first = _add_memory(workspace=tmp_path, content="first", kind="project", tags=["runtime"])
    _add_memory(workspace=tmp_path, content="second", kind="feedback", tags=["runtime"])
    second = _add_memory(workspace=tmp_path, content="third", kind="project", tags=["runtime"])

    result = _invoke(
        _memory_tools()["memory_list"],
        {"limit": 20, "kind": "project", "tags": ["runtime"]},
        tmp_path,
    )

    assert result.status == "ok"
    memories = _read_result_items(result, "memories")
    assert [memory["id"] for memory in memories] == [second["id"], first["id"]]


def test_memory_delete_tombstones_memory_and_hides_it_from_search_and_list(tmp_path: Path) -> None:
    memory = _add_memory(workspace=tmp_path, content="delete me")

    delete_result = _invoke(_memory_tools()["memory_delete"], {"id": memory["id"]}, tmp_path)
    search_result = _invoke(_memory_tools()["memory_search"], {"query": "delete me"}, tmp_path)
    list_result = _invoke(_memory_tools()["memory_list"], {}, tmp_path)

    assert delete_result.status == "ok"
    assert delete_result.data == {"id": memory["id"], "deleted": True, "tombstoned": True}
    assert search_result.data["results"] == []
    assert all(item["id"] != memory["id"] for item in _read_result_items(list_result, "memories"))


def test_memory_delete_reports_unknown_id_without_creating_tombstone(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown memory id"):
        _invoke(_memory_tools()["memory_delete"], {"id": "mem_missing"}, tmp_path)

    result = _invoke(_memory_tools()["memory_list"], {}, tmp_path)
    assert result.data["memories"] == []


def test_memory_tools_isolate_runtime_workspaces(tmp_path: Path) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    _add_memory(workspace=workspace_a, content="workspace a only")
    _add_memory(workspace=workspace_b, content="workspace b only")

    result_a = _invoke(_memory_tools()["memory_search"], {"query": "workspace"}, workspace_a)
    result_b = _invoke(_memory_tools()["memory_search"], {"query": "workspace"}, workspace_b)

    assert [item["content"] for item in _read_result_items(result_a, "results")] == [
        "workspace a only"
    ]
    assert [item["content"] for item in _read_result_items(result_b, "results")] == [
        "workspace b only"
    ]


@pytest.mark.parametrize("tool_name", ["memory_search", "memory_list"])
@pytest.mark.parametrize("limit", [0, -1, 101, "5"])
def test_memory_read_tools_reject_invalid_limits(
    tmp_path: Path, tool_name: str, limit: object
) -> None:
    base_arguments = {"memory_search": {"query": "x"}, "memory_list": {}}[tool_name]

    with pytest.raises(ValueError, match="limit"):
        _invoke(_memory_tools()[tool_name], {**base_arguments, "limit": limit}, tmp_path)


def test_memory_search_rejects_empty_query(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="query"):
        _invoke(_memory_tools()["memory_search"], {"query": "   "}, tmp_path)
