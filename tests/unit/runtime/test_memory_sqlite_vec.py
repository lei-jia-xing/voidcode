from __future__ import annotations

import builtins
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest


def _load_memory_symbols() -> tuple[Any, ...]:
    module: Any = import_module("voidcode.runtime.memory")
    return (
        module.MemoryConfig,
        module.SqliteVecCapability,
        module.MemorySearchQuery,
        module.build_memory_manager,
        module.detect_sqlite_vec_capability,
    )


def _memory_config() -> Any:
    return _load_memory_symbols()[0]


def _sqlite_vec_capability() -> Any:
    return _load_memory_symbols()[1]


def _memory_search_query() -> Any:
    return _load_memory_symbols()[2]


def _build_memory_manager() -> Any:
    return _load_memory_symbols()[3]


def _detect_sqlite_vec_capability() -> Any:
    return _load_memory_symbols()[4]


def test_sqlite_vec_capability_reports_available_when_extension_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_connections: list[object] = []

    class FakeConnection:
        def enable_load_extension(self, enabled: bool) -> None:
            self.enabled = enabled

    class FakeSqliteVecModule:
        @staticmethod
        def load(connection: object) -> None:
            loaded_connections.append(connection)

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 41, 0))
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setitem(sys.modules, "sqlite_vec", FakeSqliteVecModule)

    capability = _detect_sqlite_vec_capability()

    assert capability == _sqlite_vec_capability()(status="available", detail=None)
    assert len(loaded_connections) == 1


def test_sqlite_vec_capability_reports_not_installed_without_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def missing_sqlite_vec(name: str, *args: Any, **kwargs: Any) -> object:
        if name == "sqlite_vec":
            raise ModuleNotFoundError("No module named 'sqlite_vec'")
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "sqlite_vec", raising=False)
    monkeypatch.setattr(builtins, "__import__", missing_sqlite_vec)
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 41, 0))

    capability = _detect_sqlite_vec_capability()

    assert capability.status == "not_installed"
    assert "sqlite_vec" in capability.detail


def test_sqlite_vec_capability_reports_extension_loading_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def enable_load_extension(self, enabled: bool) -> None:
            _ = enabled
            raise sqlite3.NotSupportedError("extension loading is disabled")

    class FakeSqliteVecModule:
        @staticmethod
        def load(connection: object) -> None:
            _ = connection
            raise AssertionError(
                "sqlite_vec.load should not run when extension loading is unavailable"
            )

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 41, 0))
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setitem(sys.modules, "sqlite_vec", FakeSqliteVecModule)

    capability = _detect_sqlite_vec_capability()()

    assert capability.status == "extension_loading_unavailable"
    assert "extension loading" in capability.detail


def test_sqlite_vec_capability_reports_macos_extension_loading_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed_connections: list[object] = []

    class FakeConnection:
        def enable_load_extension(self, enabled: bool) -> None:
            _ = enabled
            raise sqlite3.NotSupportedError("extension loading is disabled on macOS")

        def close(self) -> None:
            closed_connections.append(self)

    class FakeSqliteVecModule:
        @staticmethod
        def load(connection: object) -> None:
            _ = connection
            raise AssertionError(
                "sqlite_vec.load should not run when macOS blocks extension loading"
            )

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 41, 0))
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setitem(sys.modules, "sqlite_vec", FakeSqliteVecModule)

    capability = _detect_sqlite_vec_capability()()

    assert capability.status == "extension_loading_unavailable"
    assert "macOS" in capability.detail
    assert len(closed_connections) == 1


def test_sqlite_vec_capability_reports_injected_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeConnection:
        def enable_load_extension(self, enabled: bool) -> None:
            self.enabled = enabled

    class FakeSqliteVecModule:
        @staticmethod
        def load(connection: object) -> None:
            _ = connection
            raise sqlite3.OperationalError("loadable extensions are unavailable")

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 41, 0))
    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setitem(sys.modules, "sqlite_vec", FakeSqliteVecModule)

    capability = _detect_sqlite_vec_capability()

    assert capability.status == "extension_loading_unavailable"
    assert "loadable extensions" in capability.detail


def test_sqlite_vec_capability_reports_sqlite_version_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSqliteVecModule:
        pass

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 40, 1))
    monkeypatch.setitem(sys.modules, "sqlite_vec", FakeSqliteVecModule)

    capability = _detect_sqlite_vec_capability()

    assert capability.status == "sqlite_version_unsupported"
    assert "3.41" in capability.detail


def test_memory_manager_reports_not_configured_when_memory_config_missing() -> None:
    manager = _build_memory_manager()(None)

    state = manager.current_state()

    assert state.mode == "disabled"
    assert state.sqlite_vec.status == "not_configured"
    assert manager.search(_memory_search_query()(text="anything", limit=5)) == ()


def test_memory_manager_reports_disabled_when_explicitly_disabled() -> None:
    manager = _build_memory_manager()(_memory_config()(enabled=False))

    state = manager.current_state()

    assert state.mode == "disabled"
    assert state.sqlite_vec.status == "disabled"
    assert manager.search(_memory_search_query()(text="anything", limit=5)) == ()


def test_memory_fallback_keyword_search_remains_usable_without_sqlite_vec(
    tmp_path: Path,
) -> None:
    capability = _sqlite_vec_capability()(
        status="not_installed", detail="sqlite_vec is not installed"
    )
    manager = _build_memory_manager()(
        _memory_config()(
            enabled=True, scope="workspace", semantic_search="auto", sqlite_vec={"enabled": "auto"}
        ),
        sqlite_vec_capability=capability,
        workspace=tmp_path,
    )
    manager.remember("alpha project uses local SQLite memory", source="notes")
    manager.remember("beta task uses remote vector memory", source="notes")

    results = manager.search(_memory_search_query()(text="alpha SQLite", limit=5))

    assert manager.current_state().sqlite_vec == capability
    assert [result.text for result in results] == ["alpha project uses local SQLite memory"]
    assert all(result.search_mode == "keyword" for result in results)
    assert all(result.embedding is None for result in results)


def test_memory_manager_fallback_search_uses_injected_sqlite_vec_capability(
    tmp_path: Path,
) -> None:
    capability = _sqlite_vec_capability()(
        status="not_installed", detail="sqlite_vec is not installed"
    )

    manager = _build_memory_manager()(
        _memory_config()(
            enabled=True,
            scope="workspace",
            semantic_search="auto",
            sqlite_vec={"enabled": "auto"},
        ),
        sqlite_vec_capability=capability,
        workspace=tmp_path,
    )
    manager.remember("alpha fallback memory", source="test")

    results = manager.search(_memory_search_query()(text="alpha fallback", limit=5))

    assert manager.current_state().sqlite_vec.status == "not_installed"
    assert [result.text for result in results] == ["alpha fallback memory"]
    assert results[0].search_mode == "keyword"


def test_semantic_search_does_not_appear_available_without_embeddings(tmp_path: Path) -> None:
    manager = _build_memory_manager()(
        _memory_config()(
            enabled=True, scope="workspace", semantic_search="auto", sqlite_vec={"enabled": "auto"}
        ),
        sqlite_vec_capability=_sqlite_vec_capability()(
            status="not_installed", detail="sqlite_vec is not installed"
        ),
        workspace=tmp_path,
    )

    state = manager.current_state()

    assert state.semantic_search_available is False
    assert state.keyword_search_available is True


def test_required_semantic_search_disables_keyword_fallback_without_embeddings(
    tmp_path: Path,
) -> None:
    manager = _build_memory_manager()(
        _memory_config()(
            enabled=True,
            scope="workspace",
            semantic_search="required",
            sqlite_vec={"enabled": "auto"},
        ),
        sqlite_vec_capability=_sqlite_vec_capability()(
            status="not_installed", detail="sqlite_vec is not installed"
        ),
        workspace=tmp_path,
    )
    manager.remember("alpha required semantic memory", source="test")

    state = manager.current_state()

    assert state.semantic_search_available is False
    assert state.keyword_search_available is False
    assert manager.search(_memory_search_query()(text="alpha", limit=5)) == ()


def test_required_sqlite_vec_disables_keyword_fallback_without_embeddings(
    tmp_path: Path,
) -> None:
    manager = _build_memory_manager()(
        _memory_config()(
            enabled=True,
            scope="workspace",
            semantic_search="auto",
            sqlite_vec={"enabled": "required"},
        ),
        sqlite_vec_capability=_sqlite_vec_capability()(
            status="extension_loading_unavailable", detail="extensions disabled"
        ),
        workspace=tmp_path,
    )
    manager.remember("alpha required sqlite-vec memory", source="test")

    state = manager.current_state()

    assert state.sqlite_vec.status == "extension_loading_unavailable"
    assert state.semantic_search_available is False
    assert state.keyword_search_available is False
    assert manager.search(_memory_search_query()(text="alpha", limit=5)) == ()


def test_off_semantic_search_keeps_keyword_search_available(tmp_path: Path) -> None:
    manager = _build_memory_manager()(
        _memory_config()(
            enabled=True,
            scope="workspace",
            semantic_search="off",
            sqlite_vec={"enabled": "off"},
        ),
        workspace=tmp_path,
    )
    manager.remember("alpha keyword memory", source="test")

    state = manager.current_state()
    results = manager.search(_memory_search_query()(text="alpha", limit=5))

    assert state.sqlite_vec.status == "disabled"
    assert state.semantic_search_available is False
    assert state.keyword_search_available is True
    assert [result.text for result in results] == ["alpha keyword memory"]


def test_base_voidcode_import_does_not_import_sqlite_vec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__
    attempted_imports: list[str] = []

    def track_sqlite_vec_import(name: str, *args: Any, **kwargs: Any) -> object:
        if name == "sqlite_vec":
            attempted_imports.append(name)
            raise AssertionError("base voidcode import must not require sqlite_vec")
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "sqlite_vec", raising=False)
    monkeypatch.setattr(builtins, "__import__", track_sqlite_vec_import)

    module = import_module("voidcode")

    assert module.__name__ == "voidcode"
    assert attempted_imports == []
