from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .memory import (
    MemoryKind,
    MemoryRecord,
    MemorySearchResult,
)

if TYPE_CHECKING:
    from .storage_shared import _StorageMixinBase

    _MixinBase = _StorageMixinBase
else:
    _MixinBase = object


class _MemoryStorageMixin(_MixinBase):
    @staticmethod
    def _validate_memory_content(content: str) -> str:
        if not content.strip():
            raise ValueError("memory content must not be empty")
        return content

    @classmethod
    def _validate_memory_kind(cls, kind: str) -> MemoryKind:
        if kind not in cls._MEMORY_KINDS:
            raise ValueError(f"invalid memory kind: {kind}")
        return kind

    @staticmethod
    def _validate_memory_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
        for tag in tags:
            if not tag.strip():
                raise ValueError("memory tags must not be empty")
        if len(set(tags)) != len(tags):
            raise ValueError("memory tags must be unique")
        return tags

    @classmethod
    def _memory_record_from_row(cls, row: sqlite3.Row) -> MemoryRecord:
        tags_payload = cast(str, row["tags_json"])
        try:
            decoded_tags = json.loads(tags_payload)
        except json.JSONDecodeError as exc:
            raise ValueError("persisted memory tags JSON is malformed") from exc
        if not isinstance(decoded_tags, list) or not all(isinstance(tag, str) for tag in decoded_tags):
            raise ValueError("persisted memory tags payload must decode to a string list")
        scope = cast(str, row["scope"])
        if scope != "workspace":
            raise ValueError(f"invalid memory scope: {scope}")
        return MemoryRecord(
            id=cast(str, row["memory_id"]),
            workspace_id=cast(str, row["workspace_id"]),
            kind=cls._parse_memory_kind(cast(str, row["kind"])),
            content=cast(str, row["content"]),
            tags=tuple(decoded_tags),
            status=cls._parse_memory_status(cast(str, row["status"])),
            scope="workspace",
            created_at=cast(int, row["created_at"]),
            updated_at=cast(int, row["updated_at"]),
            deleted_at=cast(int | None, row["deleted_at"]),
            source_session_id=cast(str | None, row["source_session_id"]),
        )

    def add_memory(
        self,
        *,
        workspace: Path,
        content: str,
        kind: MemoryKind = "project",
        tags: tuple[str, ...] = (),
        source_session_id: str | None = None,
    ) -> MemoryRecord:
        validated_content = self._validate_memory_content(content)
        validated_kind = self._validate_memory_kind(kind)
        validated_tags = self._validate_memory_tags(tags)
        with self._write_connect(workspace) as connection:
            timestamp = self._next_memory_timestamp(connection=connection)
            memory_id = f"mem_{timestamp}"
            _ = connection.execute(
                """
                INSERT INTO memories (
                    memory_id, workspace_id, kind, content, tags_json, scope, status,
                    source_session_id, created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, 'workspace', 'active', ?, ?, ?, NULL)
                """,
                (
                    memory_id,
                    str(workspace),
                    validated_kind,
                    validated_content,
                    json.dumps(list(validated_tags), sort_keys=True),
                    source_session_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        record = self.get_memory(workspace=workspace, memory_id=memory_id)
        if record is None:
            raise RuntimeError(f"memory was not persisted: {memory_id}")
        return record

    def list_memories(self, *, workspace: Path, include_deleted: bool = False) -> tuple[MemoryRecord, ...]:
        status_clause = "" if include_deleted else "AND status = 'active'"
        with self._connect(workspace) as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    f"""
                    SELECT memory_id, workspace_id, kind, content, tags_json, scope, status,
                           source_session_id, created_at, updated_at, deleted_at
                    FROM memories
                    WHERE workspace_id = ? {status_clause}
                    ORDER BY updated_at DESC, memory_id ASC
                    """,
                    (str(workspace),),
                ).fetchall(),
            )
        return tuple(self._memory_record_from_row(row) for row in rows)

    @staticmethod
    def _memory_search_terms(query: str) -> tuple[str, ...]:
        terms: list[str] = []
        seen: set[str] = set()
        for raw_term in query.casefold().split():
            term = raw_term.strip()
            if term and term not in seen:
                terms.append(term)
                seen.add(term)
        return tuple(terms)

    @staticmethod
    def _score_memory(record: MemoryRecord, terms: tuple[str, ...]) -> tuple[int, tuple[str, ...]]:
        haystacks = (record.content.casefold(), *(tag.casefold() for tag in record.tags))
        matched_terms = tuple(term for term in terms if any(term in haystack for haystack in haystacks))
        score = sum(haystack.count(term) for term in terms for haystack in haystacks)
        return score, matched_terms

    def search_memories(self, *, workspace: Path, query: str) -> tuple[MemorySearchResult, ...]:
        terms = self._memory_search_terms(query)
        if not terms:
            return ()
        results: list[MemorySearchResult] = []
        for record in self.list_memories(workspace=workspace):
            score, matched_terms = self._score_memory(record, terms)
            if score == 0:
                continue
            results.append(MemorySearchResult(record=record, score=score, matched_terms=matched_terms))
        return tuple(
            sorted(
                results,
                key=lambda result: (-result.score, -result.record.updated_at, result.record.id),
            )
        )

    def get_memory(self, *, workspace: Path, memory_id: str) -> MemoryRecord | None:
        with self._connect(workspace) as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT memory_id, workspace_id, kind, content, tags_json, scope, status,
                           source_session_id, created_at, updated_at, deleted_at
                    FROM memories
                    WHERE workspace_id = ? AND memory_id = ? AND status = 'active'
                    """,
                    (str(workspace), memory_id),
                ).fetchone(),
            )
        return None if row is None else self._memory_record_from_row(row)

    def delete_memory(self, *, workspace: Path, memory_id: str) -> MemoryRecord:
        with self._write_connect(workspace) as connection:
            existing = self._memory_row(
                connection=connection,
                workspace=workspace,
                memory_id=memory_id,
                include_deleted=False,
            )
            if existing is None:
                raise ValueError(f"unknown memory: {memory_id}")
            timestamp = self._next_memory_timestamp(connection=connection)
            _ = connection.execute(
                """
                UPDATE memories
                SET status = 'deleted', updated_at = ?, deleted_at = ?
                WHERE workspace_id = ? AND memory_id = ? AND status = 'active'
                """,
                (timestamp, timestamp, str(workspace), memory_id),
            )
            deleted = self._memory_row(
                connection=connection,
                workspace=workspace,
                memory_id=memory_id,
                include_deleted=True,
            )
            connection.commit()
        if deleted is None:
            raise RuntimeError(f"memory was not tombstoned: {memory_id}")
        return self._memory_record_from_row(deleted)

    @staticmethod
    def _memory_row(
        *,
        connection: sqlite3.Connection,
        workspace: Path,
        memory_id: str,
        include_deleted: bool,
    ) -> sqlite3.Row | None:
        status_clause = "" if include_deleted else "AND status = 'active'"
        return cast(
            sqlite3.Row | None,
            connection.execute(
                f"""
                SELECT memory_id, workspace_id, kind, content, tags_json, scope, status,
                       source_session_id, created_at, updated_at, deleted_at
                FROM memories
                WHERE workspace_id = ? AND memory_id = ? {status_clause}
                """,
                (str(workspace), memory_id),
            ).fetchone(),
        )

    def _next_memory_timestamp(self, *, connection: sqlite3.Connection) -> int:
        return self._next_sequence_value(connection=connection, scope="memories")
