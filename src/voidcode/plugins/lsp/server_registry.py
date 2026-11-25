"""Server runtime records, session-scoped sharing, and lease bookkeeping."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from voidcode.capabilities.workspace_queries import PositionEncoding

from .contracts import ResolvedLspServerConfig

type LocalServerKey = tuple[str, str, Path]


@dataclass(frozen=True, slots=True)
class OpenedLspDocument:
    version: int
    content_hash: str


@dataclass(slots=True)
class RunningLspServer:
    config: ResolvedLspServerConfig
    process_id: str
    workspace_root: Path
    stdout_cursor: int = 0
    initialized: bool = False
    next_request_id: int = 1
    position_encoding: PositionEncoding = "utf-16"
    document_sync_enabled: bool = False
    documents: dict[str, OpenedLspDocument] = field(default_factory=dict)


@dataclass(slots=True)
class SharedLspServerEntry:
    server: RunningLspServer
    generation: int
    ref_count: int = 0


@dataclass(frozen=True, slots=True)
class LspRegistryKey:
    session_id: str
    server_name: str
    workspace_root: Path


@dataclass(frozen=True, slots=True)
class LspServerLease:
    key: LspRegistryKey
    generation: int


class SessionScopedLspRegistry:
    """Session-scoped server sharing; the manager's owner thread serializes manager calls."""

    def __init__(self) -> None:
        self._entries: dict[LspRegistryKey, SharedLspServerEntry] = {}
        self._next_generation = 1
        self._lock = threading.Lock()

    def _allocate_generation(self) -> int:
        generation = self._next_generation
        self._next_generation += 1
        return generation

    def acquire(
        self,
        *,
        key: LspRegistryKey,
        config: ResolvedLspServerConfig,
        start: Callable[[str], RunningLspServer],
    ) -> tuple[RunningLspServer, LspServerLease, Literal["started", "reused"]]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if entry.server.config != config:
                    raise ValueError(
                        "session-scoped LSP reuse rejected because the existing "
                        f"{key.server_name} server for {key.workspace_root} was started with "
                        "a different resolved configuration"
                    )
                entry.ref_count += 1
                return (
                    entry.server,
                    LspServerLease(key=key, generation=entry.generation),
                    "reused",
                )

            server = start(f"lsp-{uuid.uuid4().hex[:12]}")
            generation = self._allocate_generation()
            self._entries[key] = SharedLspServerEntry(
                server=server,
                generation=generation,
                ref_count=1,
            )
            return server, LspServerLease(key=key, generation=generation), "started"

    def release(self, *, lease: LspServerLease) -> RunningLspServer | None:
        with self._lock:
            entry = self._entries.get(lease.key)
            if entry is None:
                return None
            if entry.generation != lease.generation:
                return None
            if entry.ref_count > 1:
                entry.ref_count -= 1
                return None
            self._entries.pop(lease.key, None)
            return entry.server

    def invalidate(self, *, key: LspRegistryKey, process_id: str) -> RunningLspServer | None:
        """Drop one entry only when it still holds this process id; never touches the host."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.server.process_id != process_id:
                return None
            self._entries.pop(key, None)
            return entry.server
