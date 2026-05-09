from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
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
class MemorySearchQuery:
    text: str
    limit: int = 5


@dataclass(frozen=True, slots=True)
class MemoryManagerState:
    mode: Literal["enabled", "disabled"]
    sqlite_vec: SqliteVecCapability
    semantic_search_available: bool
    keyword_search_available: bool


@dataclass(frozen=True, slots=True)
class MemoryManagerSearchResult:
    text: str
    search_mode: Literal["keyword"]
    score: int
    embedding: None = None


class MemoryManager(Protocol):
    def current_state(self) -> MemoryManagerState: ...

    def remember(self, text: str, *, source: str | None = None) -> None: ...

    def search(self, query: MemorySearchQuery) -> tuple[MemoryManagerSearchResult, ...]: ...


@dataclass(slots=True)
class _KeywordMemoryManager:
    config: MemoryConfig | None
    sqlite_vec_capability: SqliteVecCapability | None = None
    workspace: Path | None = None
    _entries: list[str] = field(default_factory=list)

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
            keyword_search_available=not (
                _semantic_search_required(self.config) and not semantic_search_available
            ),
        )

    def remember(self, text: str, *, source: str | None = None) -> None:
        _ = source
        normalized = text.strip()
        if self.config is None or not self.config.enabled or not normalized:
            return
        self._entries.append(normalized)

    def search(self, query: MemorySearchQuery) -> tuple[MemoryManagerSearchResult, ...]:
        state = self.current_state()
        if not state.keyword_search_available:
            return ()
        terms = _keyword_terms(query.text)
        if not terms:
            return ()
        results: list[MemoryManagerSearchResult] = []
        for entry in self._entries:
            entry_folded = entry.casefold()
            score = sum(entry_folded.count(term) for term in terms)
            if score:
                results.append(
                    MemoryManagerSearchResult(text=entry, search_mode="keyword", score=score)
                )
        ordered = sorted(
            results,
            key=lambda result: (-result.score, self._entries.index(result.text)),
        )
        return tuple(ordered[: query.limit])


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


def _keyword_terms(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    terms: list[str] = []
    for raw_term in text.casefold().split():
        term = raw_term.strip()
        if term and term not in seen:
            terms.append(term)
            seen.add(term)
    return tuple(terms)


def _capability_for_config(config: MemoryConfig) -> SqliteVecCapability:
    if config.sqlite_vec.enabled == "off":
        return SqliteVecCapability(status="disabled", detail=None)
    return detect_sqlite_vec_capability()


def _detect_sqlite_vec_capability() -> SqliteVecCapability:
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


class _SqliteVecCapabilityDetector:
    def __call__(self) -> SqliteVecCapability:
        return _detect_sqlite_vec_capability()

    def __getattr__(self, name: str) -> object:
        return getattr(self(), name)

    def __eq__(self, other: object) -> bool:
        return self() == other


detect_sqlite_vec_capability = _SqliteVecCapabilityDetector()


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
