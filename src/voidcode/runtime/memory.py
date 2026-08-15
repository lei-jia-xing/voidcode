from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

MemoryKind = Literal["project", "preference", "feedback", "reference", "decision"]
MemoryStatus = Literal["active", "deleted"]
MemoryScope = Literal["workspace"]
MemorySemanticSearchMode = Literal["off", "auto", "required"]
MemorySqliteVecMode = Literal["auto", "off", "required"]
SqliteVecCapabilityStatus = Literal[
    "available",
    "not_installed",
    "extension_loading_unavailable",
    "sqlite_version_unsupported",
    "not_configured",
    "disabled",
]


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    workspace_id: str
    kind: MemoryKind
    content: str
    tags: tuple[str, ...] = ()
    status: MemoryStatus = "active"
    scope: MemoryScope = "workspace"
    created_at: int = 0
    updated_at: int = 0
    deleted_at: int | None = None
    source_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    record: MemoryRecord
    score: int
    matched_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemoryRecallConfig:
    enabled: bool = False
    limit: int = 5
    max_chars: int = 2000


@dataclass(frozen=True, slots=True)
class MemorySqliteVecConfig:
    enabled: MemorySqliteVecMode = "auto"


@dataclass(frozen=True, slots=True, init=False)
class MemoryConfig:
    enabled: bool
    scope: MemoryScope
    recall: MemoryRecallConfig
    semantic_search: MemorySemanticSearchMode
    sqlite_vec: MemorySqliteVecConfig

    def __init__(
        self,
        *,
        enabled: bool = True,
        scope: MemoryScope = "workspace",
        recall: MemoryRecallConfig | dict[str, object] | None = None,
        semantic_search: MemorySemanticSearchMode = "auto",
        sqlite_vec: MemorySqliteVecConfig | dict[str, object] | None = None,
    ) -> None:
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "recall", _memory_recall_config_from_value(recall))
        object.__setattr__(self, "semantic_search", semantic_search)
        object.__setattr__(self, "sqlite_vec", _memory_sqlite_vec_config_from_value(sqlite_vec))


@dataclass(frozen=True, slots=True)
class SqliteVecCapability:
    status: SqliteVecCapabilityStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryManagerState:
    mode: Literal["enabled", "disabled"]
    sqlite_vec: SqliteVecCapability
    semantic_search_available: bool
    keyword_search_available: bool


class MemoryManager(Protocol):
    def current_state(self) -> MemoryManagerState: ...


@dataclass(slots=True)
class _KeywordMemoryManager:
    config: MemoryConfig | None
    sqlite_vec_capability: SqliteVecCapability | None = None
    workspace: Path | None = None

    def current_state(self) -> MemoryManagerState:
        if self.config is None:
            return MemoryManagerState(
                mode="disabled",
                sqlite_vec=SqliteVecCapability(status="not_configured", detail=None),
                semantic_search_available=False,
                keyword_search_available=False,
            )
        if not self.config.enabled:
            return MemoryManagerState(
                mode="disabled",
                sqlite_vec=SqliteVecCapability(status="disabled", detail=None),
                semantic_search_available=False,
                keyword_search_available=False,
            )
        capability = self.sqlite_vec_capability or _capability_for_config(self.config)
        semantic_search_available = False
        return MemoryManagerState(
            mode="enabled",
            sqlite_vec=capability,
            semantic_search_available=semantic_search_available,
            keyword_search_available=not (_semantic_search_required(self.config) and not semantic_search_available),
        )


def _memory_recall_config_from_value(
    value: MemoryRecallConfig | dict[str, object] | None,
) -> MemoryRecallConfig:
    if value is None:
        return MemoryRecallConfig()
    if isinstance(value, MemoryRecallConfig):
        return value
    return MemoryRecallConfig(
        enabled=cast(bool, value.get("enabled", False)),
        limit=cast(int, value.get("limit", 5)),
        max_chars=cast(int, value.get("max_chars", 2000)),
    )


def _memory_sqlite_vec_config_from_value(
    value: MemorySqliteVecConfig | dict[str, object] | None,
) -> MemorySqliteVecConfig:
    if value is None:
        return MemorySqliteVecConfig()
    if isinstance(value, MemorySqliteVecConfig):
        return value
    return MemorySqliteVecConfig(enabled=cast(MemorySqliteVecMode, value.get("enabled", "auto")))


def _semantic_search_required(config: MemoryConfig) -> bool:
    return config.semantic_search == "required" or config.sqlite_vec.enabled == "required"


def _capability_for_config(config: MemoryConfig) -> SqliteVecCapability:
    if config.sqlite_vec.enabled == "off":
        return SqliteVecCapability(status="disabled", detail=None)
    return detect_sqlite_vec_capability()


def detect_sqlite_vec_capability() -> SqliteVecCapability:
    if sqlite3.sqlite_version_info < (3, 41, 0):
        return SqliteVecCapability(
            status="sqlite_version_unsupported",
            detail=f"sqlite-vec requires SQLite 3.41 or newer; found {sqlite3.sqlite_version}",
        )

    try:
        sqlite_vec = __import__("sqlite_vec")
    except ModuleNotFoundError as exc:
        return SqliteVecCapability(status="not_installed", detail=str(exc))

    try:
        connection = sqlite3.connect(":memory:")
    except sqlite3.Error as exc:
        return SqliteVecCapability(status="extension_loading_unavailable", detail=str(exc))

    try:
        try:
            connection.enable_load_extension(True)
        except (AttributeError, sqlite3.Error) as exc:
            return SqliteVecCapability(status="extension_loading_unavailable", detail=str(exc))
        try:
            load_extension = sqlite_vec.load
            load_extension(connection)
        except sqlite3.Error as exc:
            return SqliteVecCapability(status="extension_loading_unavailable", detail=str(exc))
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    return SqliteVecCapability(status="available", detail=None)


def build_memory_manager(
    config: MemoryConfig | None,
    *,
    sqlite_vec_capability: SqliteVecCapability | None = None,
    workspace: Path | None = None,
) -> MemoryManager:
    return _KeywordMemoryManager(
        config=config,
        sqlite_vec_capability=sqlite_vec_capability,
        workspace=workspace,
    )
