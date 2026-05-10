from __future__ import annotations

import importlib
import sqlite3
import sys
import threading
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.events import (
    KNOWN_EVENT_TYPES,
    RUNTIME_MEMORY_ADDED,
    RUNTIME_MEMORY_DELETED,
    RUNTIME_MEMORY_SEARCHED,
    RUNTIME_MEMORY_STATUS_CHECKED,
)
from voidcode.runtime.memory import (
    MemoryConfig,
    MemoryKind,
    MemoryRecallConfig,
    SqliteVecCapability,
)
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore

storage_module = importlib.import_module("voidcode.runtime.storage")


def _sqlite_artifacts(workspace: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for path in workspace.rglob("*")
        if path.suffix in {".sqlite", ".sqlite3", ".db"}
        or "sqlite-vec" in path.name
        or "vector" in path.name.lower()
    )


def _memory_table_columns(
    database_path: Path, table_name: str
) -> list[tuple[str, str, int, str | None, int]]:
    with closing(sqlite3.connect(database_path)) as connection:
        return [
            (row[1], row[2], row[3], row[4], row[5])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]


def _memory_table_names(database_path: Path) -> set[str]:
    with closing(sqlite3.connect(database_path)) as connection:
        return {
            cast(str, row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _memory_indexes(database_path: Path, table_name: str) -> set[tuple[str, bool, tuple[str, ...]]]:
    with closing(sqlite3.connect(database_path)) as connection:
        indexes: set[tuple[str, bool, tuple[str, ...]]] = set()
        for row in connection.execute(f"PRAGMA index_list({table_name})").fetchall():
            index_name = cast(str, row[1])
            columns = tuple(
                cast(str, column_row[2])
                for column_row in connection.execute(f"PRAGMA index_info({index_name})").fetchall()
            )
            indexes.add((index_name, bool(row[2]), columns))
        return indexes


def _add_memory(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    content: str,
    kind: MemoryKind = "project",
    tags: Iterable[str] = (),
    source_session_id: str | None = None,
) -> Any:
    return store.add_memory(
        workspace=workspace,
        content=content,
        kind=kind,
        tags=tuple(tags),
        source_session_id=source_session_id,
    )


def _list_memories(
    store: SqliteSessionStore, *, workspace: Path, include_deleted: bool = False
) -> Any:
    return store.list_memories(workspace=workspace, include_deleted=include_deleted)


def _search_memories(store: SqliteSessionStore, *, workspace: Path, query: str) -> Any:
    return store.search_memories(workspace=workspace, query=query)


def _get_memory(store: SqliteSessionStore, *, workspace: Path, memory_id: str) -> Any:
    return store.get_memory(workspace=workspace, memory_id=memory_id)


def _delete_memory(store: SqliteSessionStore, *, workspace: Path, memory_id: str) -> Any:
    return store.delete_memory(workspace=workspace, memory_id=memory_id)


def test_memory_storage_exports_runtime_contract_types() -> None:
    memory_kind = storage_module.MemoryKind
    memory_status = storage_module.MemoryStatus
    memory_record = storage_module.MemoryRecord
    memory_search_result = storage_module.MemorySearchResult

    assert memory_kind.__args__ == ("project", "preference", "feedback", "reference", "decision")
    assert memory_status.__args__ == ("active", "deleted")

    record = memory_record(
        id="mem_1",
        workspace_id="/workspace",
        kind="project",
        content="Use pytest for runtime storage tests.",
        tags=("runtime", "storage"),
        status="active",
        scope="workspace",
        created_at=1,
        updated_at=1,
        deleted_at=None,
        source_session_id="session-1",
    )
    result = memory_search_result(record=record, score=3, matched_terms=("runtime",))

    assert record.scope == "workspace"
    assert result.record == record
    assert result.score == 3
    assert result.matched_terms == ("runtime",)


def test_memory_storage_protocol_methods_exist() -> None:
    store = SqliteSessionStore(database_path=Path("/tmp/memory-contract.sqlite3"))

    for method_name in (
        "add_memory",
        "list_memories",
        "search_memories",
        "get_memory",
        "delete_memory",
    ):
        method = getattr(store, method_name)
        assert callable(method)


def test_memory_storage_bootstraps_canonical_schema_in_runtime_database(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime-state" / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)

    _add_memory(store, workspace=tmp_path / "workspace", content="Remember runtime ownership.")

    assert "memories" in _memory_table_names(database_path)
    assert {"memory_tags", "memory_recall_log", "memory_index_status"}.issubset(
        _memory_table_names(database_path)
    )
    assert _memory_table_columns(database_path, "memories") == [
        ("memory_id", "TEXT", 1, None, 2),
        ("workspace_id", "TEXT", 1, None, 1),
        ("kind", "TEXT", 1, None, 0),
        ("content", "TEXT", 1, None, 0),
        ("tags_json", "TEXT", 1, None, 0),
        ("scope", "TEXT", 1, "'workspace'", 0),
        ("status", "TEXT", 1, "'active'", 0),
        ("source_session_id", "TEXT", 0, None, 0),
        ("created_at", "INTEGER", 1, None, 0),
        ("updated_at", "INTEGER", 1, None, 0),
        ("deleted_at", "INTEGER", 0, None, 0),
    ]
    assert _memory_table_columns(database_path, "memory_tags") == [
        ("workspace_id", "TEXT", 1, None, 1),
        ("memory_id", "TEXT", 1, None, 2),
        ("tag", "TEXT", 1, None, 3),
        ("created_at", "INTEGER", 1, None, 0),
    ]
    assert _memory_table_columns(database_path, "memory_recall_log") == [
        ("workspace_id", "TEXT", 1, None, 1),
        ("recall_id", "TEXT", 1, None, 2),
        ("session_id", "TEXT", 0, None, 0),
        ("query", "TEXT", 0, None, 0),
        ("result_count", "INTEGER", 1, "0", 0),
        ("created_at", "INTEGER", 1, None, 0),
    ]
    assert _memory_table_columns(database_path, "memory_index_status") == [
        ("workspace_id", "TEXT", 1, None, 1),
        ("index_name", "TEXT", 1, None, 2),
        ("status", "TEXT", 1, None, 0),
        ("detail_json", "TEXT", 1, "'{}'", 0),
        ("updated_at", "INTEGER", 1, None, 0),
    ]
    assert any(
        columns == ("workspace_id", "status", "updated_at")
        for _, _, columns in _memory_indexes(database_path, "memories")
    )
    assert any(
        columns == ("workspace_id", "tag")
        for _, _, columns in _memory_indexes(database_path, "memory_tags")
    )
    assert any(
        columns == ("workspace_id", "created_at")
        for _, _, columns in _memory_indexes(database_path, "memory_recall_log")
    )
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


def test_memory_storage_rejects_version_mismatch_without_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "future-runtime.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute("PRAGMA user_version = 999")
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)

    with pytest.raises(
        RuntimeError,
        match=r"schema version mismatch: expected 6 got 999.*future-runtime\.sqlite3",
    ):
        _list_memories(store, workspace=tmp_path)


def test_memory_storage_rejects_unversioned_non_canonical_memory_schema_without_stamping(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-runtime.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        _ = connection.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, workspace TEXT NOT NULL)"
        )
        connection.commit()

    store = SqliteSessionStore(database_path=database_path)

    with pytest.raises(
        RuntimeError,
        match=r"table 'memories' missing columns: .*workspace_id.*legacy-runtime\.sqlite3",
    ):
        _list_memories(store, workspace=tmp_path)

    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_memory_storage_crud_search_and_tombstone_lifecycle(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "state" / "sessions.sqlite3")
    workspace = tmp_path / "workspace"

    older = _add_memory(
        store,
        workspace=workspace,
        content="Prefer pytest for runtime storage coverage.",
        kind="preference",
        tags=("tests", "runtime"),
        source_session_id="session-1",
    )
    newer = _add_memory(
        store,
        workspace=workspace,
        content="Runtime memory belongs in the shared state database.",
        kind="decision",
        tags=("runtime", "memory"),
        source_session_id="session-2",
    )

    assert older.workspace_id == str(workspace)
    assert newer.workspace_id == str(workspace)
    assert older.id != newer.id
    assert _get_memory(store, workspace=workspace, memory_id=older.id) == older
    assert _list_memories(store, workspace=workspace) == (newer, older)

    search_results = _search_memories(store, workspace=workspace, query="runtime state")
    assert [result.record.id for result in search_results] == [newer.id, older.id]
    assert search_results[0].score > search_results[1].score
    assert search_results[0].matched_terms == ("runtime", "state")

    deleted = _delete_memory(store, workspace=workspace, memory_id=older.id)

    assert deleted.id == older.id
    assert deleted.status == "deleted"
    assert deleted.deleted_at is not None
    assert _get_memory(store, workspace=workspace, memory_id=older.id) is None
    assert _list_memories(store, workspace=workspace) == (newer,)
    assert [
        result.record.id for result in _search_memories(store, workspace=workspace, query="pytest")
    ] == []
    assert _list_memories(store, workspace=workspace, include_deleted=True) == (deleted, newer)


def test_memory_storage_validates_boundary_inputs(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "state.sqlite3")

    invalid_cases: tuple[tuple[dict[str, Any], str], ...] = (
        ({"content": ""}, "memory content must not be empty"),
        ({"content": "   "}, "memory content must not be empty"),
        ({"content": "valid", "kind": "note"}, "invalid memory kind"),
        ({"content": "valid", "tags": ("runtime", "")}, "memory tags must not be empty"),
        ({"content": "valid", "tags": ("runtime", "runtime")}, "memory tags must be unique"),
    )

    for kwargs, message in invalid_cases:
        with pytest.raises(ValueError, match=message):
            _add_memory(store, workspace=tmp_path, **kwargs)


def test_memory_storage_isolates_workspaces_by_literal_workspace_string(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "state.sqlite3")
    workspace = tmp_path / "project"
    symlink_workspace = tmp_path / "project-link"
    workspace.mkdir()
    try:
        symlink_workspace.symlink_to(workspace, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise

    original = _add_memory(store, workspace=workspace, content="Original workspace memory")
    linked = _add_memory(store, workspace=symlink_workspace, content="Symlink workspace memory")

    assert original.workspace_id == str(workspace)
    assert linked.workspace_id == str(symlink_workspace)
    assert _list_memories(store, workspace=workspace) == (original,)
    assert _list_memories(store, workspace=symlink_workspace) == (linked,)
    assert _search_memories(store, workspace=workspace, query="Symlink") == ()


def test_memory_storage_simulated_windows_keeps_literal_workspace_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    store = SqliteSessionStore(database_path=tmp_path / "state" / "sessions.sqlite3")
    workspace = tmp_path / "Project With Spaces"
    workspace.mkdir()

    memory = _add_memory(store, workspace=workspace, content="Windows literal path memory")
    results = _search_memories(store, workspace=workspace, query="Windows")

    assert memory.workspace_id == str(workspace)
    assert "Project With Spaces" in memory.workspace_id
    assert [result.record for result in results] == [memory]
    assert _sqlite_artifacts(workspace) == ()


def test_memory_storage_spaces_and_symlink_like_workspaces_stay_isolated(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "state" / "sessions.sqlite3")
    workspace = tmp_path / "project with spaces"
    symlink_workspace = tmp_path / "project with spaces link"
    workspace.mkdir()
    try:
        symlink_workspace.symlink_to(workspace, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise

    original = _add_memory(store, workspace=workspace, content="Original spaced workspace memory")
    linked = _add_memory(store, workspace=symlink_workspace, content="Linked workspace memory")

    assert original.workspace_id == str(workspace)
    assert linked.workspace_id == str(symlink_workspace)
    assert original.workspace_id != linked.workspace_id
    assert _list_memories(store, workspace=workspace) == (original,)
    assert _list_memories(store, workspace=symlink_workspace) == (linked,)
    assert _search_memories(store, workspace=workspace, query="Linked") == ()
    assert _search_memories(store, workspace=symlink_workspace, query="Original") == ()
    assert _sqlite_artifacts(workspace) == ()
    assert _sqlite_artifacts(symlink_workspace) == ()


def test_memory_storage_keyword_search_does_not_require_fts5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = sqlite3.connect
    rejected_sql: list[str] = []

    class NoFtsConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            normalized = sql.casefold()
            if "fts" in normalized or " match " in normalized:
                rejected_sql.append(sql)
                raise sqlite3.OperationalError("FTS5 is unavailable")
            return super().execute(sql, parameters)

    def connect_without_fts(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = NoFtsConnection
        return cast(Any, original_connect)(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_without_fts)
    store = SqliteSessionStore(database_path=tmp_path / "state" / "sessions.sqlite3")
    workspace = tmp_path / "workspace"
    memory = _add_memory(
        store,
        workspace=workspace,
        content="Plain SQL keyword memory survives without FTS5.",
        tags=("fallback",),
    )

    results = _search_memories(store, workspace=workspace, query="keyword fallback")

    assert [result.record for result in results] == [memory]
    assert results[0].matched_terms == ("keyword", "fallback")
    assert rejected_sql == []
    assert _sqlite_artifacts(workspace) == ()


def test_memory_storage_search_ordering_is_deterministic(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "state.sqlite3")
    workspace = tmp_path / "workspace"
    least_recent = _add_memory(store, workspace=workspace, content="alpha beta")
    most_relevant_old = _add_memory(store, workspace=workspace, content="alpha beta beta")
    most_relevant_new = _add_memory(store, workspace=workspace, content="alpha beta beta")
    newest_low_relevance = _add_memory(store, workspace=workspace, content="alpha")

    results = _search_memories(store, workspace=workspace, query="alpha beta")

    assert [result.record.id for result in results] == [
        most_relevant_new.id,
        most_relevant_old.id,
        least_recent.id,
        newest_low_relevance.id,
    ]


def test_memory_storage_concurrent_adds_allocate_unique_ordered_records(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "state.sqlite3")
    workspace = tmp_path / "workspace"
    barrier = threading.Barrier(4)
    records: list[Any] = []
    errors: list[BaseException] = []

    def add_record(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            records.append(
                _add_memory(store, workspace=workspace, content=f"concurrent memory {index}")
            )
        except BaseException as exc:  # pragma: no cover - test captures unexpected thread failures
            errors.append(exc)

    threads = [threading.Thread(target=add_record, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    listed = _list_memories(store, workspace=workspace)

    assert errors == []
    assert len(records) == 4
    assert len({record.id for record in records}) == 4
    assert [record.updated_at for record in listed] == sorted(
        (record.updated_at for record in records), reverse=True
    )


def test_memory_storage_never_creates_workspace_local_sqlite_or_vector_files(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "state" / "sessions.sqlite3"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SqliteSessionStore(database_path=database_path)

    before = _sqlite_artifacts(workspace)
    memory = _add_memory(store, workspace=workspace, content="Keep memory outside the workspace.")
    results = _search_memories(store, workspace=workspace, query="memory")

    assert before == ()
    assert results[0].record == memory
    assert _sqlite_artifacts(workspace) == ()
    assert database_path.exists()
    assert database_path.is_relative_to(tmp_path / "state")


def test_runtime_service_exposes_storage_backed_memory_api_and_status(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "sessions.sqlite3"
    workspace = tmp_path / "workspace"
    runtime = VoidCodeRuntime(
        workspace=workspace,
        session_store=SqliteSessionStore(database_path=database_path),
    )

    memory = runtime.add_memory(
        content="Runtime service owns the stable memory boundary.",
        kind="decision",
        tags=("runtime", "memory"),
        source_session_id="session-1",
    )
    status_after_add = runtime.memory_status()
    search_results = runtime.search_memories(query="stable memory")
    fetched = runtime.get_memory(memory.id)
    listed = runtime.list_memories()
    deleted = runtime.delete_memory(memory.id)
    status_after_delete = runtime.current_status().memory

    assert memory.workspace_id == str(workspace.resolve())
    assert memory.source_session_id == "session-1"
    assert fetched == memory
    assert listed == (memory,)
    assert [result.record.id for result in search_results] == [memory.id]
    assert deleted.status == "deleted"
    assert runtime.get_memory(memory.id) is None
    assert status_after_add.workspace_id == str(workspace.resolve())
    assert status_after_add.database_path == str(database_path)
    assert status_after_add.enabled is True
    assert status_after_add.active_count == 1
    assert status_after_add.deleted_count == 0
    assert status_after_add.total_count == 1
    assert status_after_add.recall_enabled is False
    assert status_after_add.semantic_search == "auto"
    assert status_after_add.sqlite_vec == "auto"
    assert status_after_add.keyword_search_available is True
    assert status_after_add.semantic_search_available is False
    assert status_after_add.sqlite_vec_status in {
        "available",
        "not_installed",
        "extension_loading_unavailable",
        "sqlite_version_unsupported",
    }
    assert status_after_delete is not None
    assert status_after_delete.active_count == 0
    assert status_after_delete.deleted_count == 1
    assert status_after_delete.total_count == 1
    assert database_path.exists()
    assert _sqlite_artifacts(workspace) == ()


def test_runtime_memory_status_reports_disabled_capabilities(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        config=RuntimeConfig(memory=MemoryConfig(enabled=False)),
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
    )

    status = runtime.memory_status()

    assert status.enabled is False
    assert status.keyword_search_available is False
    assert status.semantic_search_available is False
    assert status.sqlite_vec_status == "disabled"
    assert status.sqlite_vec_detail is None


def test_runtime_memory_operations_require_enabled_memory(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        config=RuntimeConfig(memory=MemoryConfig(enabled=False)),
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
    )

    assert runtime.memory_status().enabled is False
    for operation in (
        lambda: runtime.add_memory(content="disabled memory write"),
        lambda: runtime.list_memories(),
        lambda: runtime.search_memories(query="disabled"),
        lambda: runtime.get_memory("mem_missing"),
        lambda: runtime.delete_memory("mem_missing"),
    ):
        with pytest.raises(RuntimeError, match="memory is disabled"):
            operation()

    assert runtime.memory_status().total_count == 0


def test_runtime_memory_status_uses_injected_sqlite_vec_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_module = importlib.import_module("voidcode.runtime.memory")
    monkeypatch.setattr(
        memory_module,
        "detect_sqlite_vec_capability",
        lambda: SqliteVecCapability(status="not_installed", detail="sqlite_vec missing"),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
    )

    status = runtime.memory_status()

    assert status.enabled is True
    assert status.keyword_search_available is True
    assert status.semantic_search_available is False
    assert status.sqlite_vec == "auto"
    assert status.sqlite_vec_status == "not_installed"
    assert status.sqlite_vec_detail == "sqlite_vec missing"


def test_runtime_required_semantic_search_refuses_keyword_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_module = importlib.import_module("voidcode.runtime.memory")
    monkeypatch.setattr(
        memory_module,
        "detect_sqlite_vec_capability",
        lambda: SqliteVecCapability(status="not_installed", detail="sqlite_vec missing"),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        config=RuntimeConfig(
            memory=MemoryConfig(
                semantic_search="required",
                sqlite_vec={"enabled": "auto"},
            )
        ),
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
    )
    runtime.add_memory(content="Required semantic search must not use keywords.")

    status = runtime.memory_status()

    assert status.enabled is True
    assert status.semantic_search == "required"
    assert status.keyword_search_available is False
    assert status.semantic_search_available is False
    assert status.sqlite_vec_status == "not_installed"
    with pytest.raises(RuntimeError, match="requires semantic search"):
        runtime.search_memories(query="semantic")


def test_runtime_required_sqlite_vec_refuses_keyword_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_module = importlib.import_module("voidcode.runtime.memory")
    monkeypatch.setattr(
        memory_module,
        "detect_sqlite_vec_capability",
        lambda: SqliteVecCapability(status="not_installed", detail="sqlite_vec missing"),
    )
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        config=RuntimeConfig(
            memory=MemoryConfig(
                semantic_search="auto",
                sqlite_vec={"enabled": "required"},
            )
        ),
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
    )
    runtime.add_memory(content="Required sqlite-vec search must not use keywords.")

    status = runtime.memory_status()

    assert status.sqlite_vec == "required"
    assert status.keyword_search_available is False
    assert status.semantic_search_available is False
    assert status.sqlite_vec_status == "not_installed"
    with pytest.raises(RuntimeError, match="requires semantic search"):
        runtime.search_memories(query="sqlite-vec")


def test_runtime_memory_event_boundaries_are_explicit_and_payloads_are_stable(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
    )
    memory = runtime.add_memory(content="Emit explicit memory boundary events.")
    results = runtime.search_memories(query="explicit")

    added_payload = runtime.memory_event_payload(action="added", memory=memory)
    searched_payload = runtime.memory_event_payload(
        action="searched",
        query="explicit",
        result_count=len(results),
    )

    assert RUNTIME_MEMORY_ADDED in KNOWN_EVENT_TYPES
    assert RUNTIME_MEMORY_DELETED in KNOWN_EVENT_TYPES
    assert RUNTIME_MEMORY_SEARCHED in KNOWN_EVENT_TYPES
    assert RUNTIME_MEMORY_STATUS_CHECKED in KNOWN_EVENT_TYPES
    assert runtime.memory_event_type("added") == RUNTIME_MEMORY_ADDED
    assert runtime.memory_event_type("deleted") == RUNTIME_MEMORY_DELETED
    assert runtime.memory_event_type("searched") == RUNTIME_MEMORY_SEARCHED
    assert runtime.memory_event_type("status_checked") == RUNTIME_MEMORY_STATUS_CHECKED
    assert added_payload == {
        "action": "added",
        "workspace_id": str(tmp_path.resolve()),
        "memory_id": memory.id,
        "kind": "project",
        "status": "active",
        "tag_count": 0,
    }
    assert searched_payload == {
        "action": "searched",
        "workspace_id": str(tmp_path.resolve()),
        "query": "explicit",
        "result_count": 1,
    }


def _workspace_memory_segments(runtime: VoidCodeRuntime) -> list[Any]:
    context = runtime._assemble_provider_context(
        prompt="continue",
        tool_results=(),
        session_metadata={"runtime_config": runtime._runtime_config_metadata()},
    )
    return [
        segment
        for segment in context.segments
        if segment.metadata is not None
        and segment.metadata.get("source") == "runtime_workspace_memory"
    ]


def test_memory_recall_is_disabled_by_default(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
    )
    runtime.add_memory(content="Default recall must stay out of prompts.")

    assert _workspace_memory_segments(runtime) == []


def test_memory_recall_renders_workspace_section_with_count_and_char_budget(
    tmp_path: Path,
) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
        config=RuntimeConfig(
            memory=MemoryConfig(
                recall=MemoryRecallConfig(enabled=True, limit=2, max_chars=220),
            )
        ),
    )
    runtime.add_memory(content="old memory should be omitted by the count limit")
    second = runtime.add_memory(content="second remembered project detail", kind="decision")
    newest = runtime.add_memory(content="newest remembered project detail", kind="preference")

    (segment,) = _workspace_memory_segments(runtime)

    assert segment.role == "system"
    assert segment.metadata == {
        "source": "runtime_workspace_memory",
        "tier": "workspace",
        "layer": "project_context",
        "section": "Workspace Memory",
    }
    assert segment.content is not None
    assert segment.content.startswith("Workspace Memory:")
    assert (
        "Memories may be stale; prefer current repository files when conflicts exist."
        in segment.content
    )
    assert newest.id in segment.content
    assert second.id in segment.content
    assert newest.content in segment.content
    assert second.content in segment.content
    assert "old memory" not in segment.content
    assert len(segment.content) <= 220


def test_memory_recall_excludes_tombstoned_records(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
        config=RuntimeConfig(memory=MemoryConfig(recall=MemoryRecallConfig(enabled=True))),
    )
    deleted = runtime.add_memory(content="deleted memory must not appear")
    active = runtime.add_memory(content="active memory should appear")
    runtime.delete_memory(deleted.id)

    (segment,) = _workspace_memory_segments(runtime)

    assert segment.content is not None
    assert active.content in segment.content
    assert deleted.content not in segment.content


def test_memory_recall_ordering_is_deterministic_newest_first(tmp_path: Path) -> None:
    runtime = VoidCodeRuntime(
        workspace=tmp_path / "workspace",
        session_store=SqliteSessionStore(database_path=tmp_path / "state.sqlite3"),
        config=RuntimeConfig(memory=MemoryConfig(recall=MemoryRecallConfig(enabled=True))),
    )
    oldest = runtime.add_memory(content="oldest stable memory")
    middle = runtime.add_memory(content="middle stable memory")
    newest = runtime.add_memory(content="newest stable memory")

    (segment,) = _workspace_memory_segments(runtime)

    assert segment.content is not None
    assert segment.content.index(newest.content) < segment.content.index(middle.content)
    assert segment.content.index(middle.content) < segment.content.index(oldest.content)
