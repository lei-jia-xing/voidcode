from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar, cast, final

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from ..runtime.memory import MemoryKind, MemoryRecord
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import RuntimeMemoryToolFacade, current_runtime_tool_context

_ALLOWED_KINDS: frozenset[MemoryKind] = frozenset(
    cast(tuple[MemoryKind, ...], ("project", "preference", "feedback", "reference", "decision"))
)
_FORBIDDEN_ARGUMENTS = frozenset(("workspace", "workspace_path", "path"))
_DEFAULT_LIMIT = 20


class _MemoryArgsBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_workspace_or_path(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        forbidden = _FORBIDDEN_ARGUMENTS.intersection(value)
        if forbidden:
            raise ValueError(f"forbidden memory argument: {sorted(forbidden)[0]}")
        return value


def _validate_kind(value: str) -> MemoryKind:
    if value not in _ALLOWED_KINDS:
        raise ValueError("kind must be one of project, preference, feedback, reference, decision")
    return cast(MemoryKind, value)


def _validate_tags(value: list[str]) -> tuple[str, ...]:
    if not all(isinstance(tag, str) for tag in value):
        raise ValueError("tags must be strings")
    normalized = tuple(tag.strip() for tag in value)
    if any(not tag for tag in normalized):
        raise ValueError("tags must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError("tags must be unique")
    return normalized


def _validate_limit(value: int) -> int:
    if value < 1 or value > 100:
        raise ValueError("limit must be between 1 and 100")
    return value


class _MemoryAddArgs(_MemoryArgsBase):
    content: object
    kind: MemoryKind = "project"
    tags: tuple[str, ...] = ()

    @field_validator("content", mode="after")
    @classmethod
    def _validate_content(cls, value: object) -> object:
        if value is None:
            raise ValueError("content must not be empty")
        if isinstance(value, str) and not value.strip():
            raise ValueError("content must not be empty")
        return value

    @field_validator("kind", mode="after")
    @classmethod
    def _validated_kind(cls, value: str) -> MemoryKind:
        return _validate_kind(value)

    @field_validator("tags", mode="after")
    @classmethod
    def _validated_tags(cls, value: list[str]) -> tuple[str, ...]:
        return _validate_tags(value)


class _MemorySearchArgs(_MemoryArgsBase):
    query: str
    limit: StrictInt = _DEFAULT_LIMIT
    kind: MemoryKind | None = None
    tags: tuple[str, ...] = ()

    @field_validator("query", mode="after")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("query must not be empty")
        return stripped

    @field_validator("limit", mode="after")
    @classmethod
    def _validated_limit(cls, value: int) -> int:
        return _validate_limit(value)

    @field_validator("kind", mode="after")
    @classmethod
    def _validated_kind(cls, value: str | None) -> MemoryKind | None:
        return None if value is None else _validate_kind(value)

    @field_validator("tags", mode="after")
    @classmethod
    def _validated_tags(cls, value: list[str]) -> tuple[str, ...]:
        return _validate_tags(value)


class _MemoryListArgs(_MemoryArgsBase):
    limit: StrictInt = _DEFAULT_LIMIT
    kind: MemoryKind | None = "project"
    tags: tuple[str, ...] = ()

    @field_validator("limit", mode="after")
    @classmethod
    def _validated_limit(cls, value: int) -> int:
        return _validate_limit(value)

    @field_validator("kind", mode="after")
    @classmethod
    def _validated_kind(cls, value: str | None) -> MemoryKind | None:
        return None if value is None else _validate_kind(value)

    @field_validator("tags", mode="after")
    @classmethod
    def _validated_tags(cls, value: list[str]) -> tuple[str, ...]:
        return _validate_tags(value)


class _MemoryDeleteArgs(_MemoryArgsBase):
    id: str

    @field_validator("id", mode="after")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("id must not be empty")
        return stripped


def _json_content(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, sort_keys=True)


def _display_content(value: str) -> object:
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _record_payload(record: MemoryRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "content": _display_content(record.content),
        "kind": record.kind,
        "tags": list(record.tags),
    }


def _matches_filters(
    record: MemoryRecord, *, kind: MemoryKind | None, tags: tuple[str, ...]
) -> bool:
    if kind is not None and record.kind != kind:
        return False
    return all(tag in record.tags for tag in tags)


class _MemoryToolBase:
    definition: ClassVar[ToolDefinition]

    def __init__(self, *, memory: RuntimeMemoryToolFacade | None = None) -> None:
        self._memory = memory

    def _resolve_memory(self) -> RuntimeMemoryToolFacade:
        if self._memory is not None:
            return self._memory
        context = current_runtime_tool_context()
        if context is not None and context.memory is not None:
            return context.memory
        raise RuntimeError(f"{self.definition.name} requires a runtime-provided memory facade")

    def _validation_error(self, exc: ValidationError) -> ValueError:
        return ValueError(format_validation_error(self.definition.name, exc))


@final
class MemoryAddTool(_MemoryToolBase):
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="memory_add",
        description="Store a workspace-scoped runtime memory.",
        input_schema={
            "content": {"type": "string"},
            "kind": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _MemoryAddArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

        record = self._resolve_memory().add_memory(
            content=_json_content(args.content),
            kind=args.kind,
            tags=args.tags,
        )
        payload = _record_payload(record)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Stored memory: {record.id}",
            data=payload,
        )


@final
class MemorySearchTool(_MemoryToolBase):
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="memory_search",
        description="Search workspace-scoped runtime memories.",
        input_schema={
            "query": {"type": "string"},
            "limit": {"type": "integer"},
            "kind": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _MemorySearchArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

        results = [
            result.record
            for result in self._resolve_memory().search_memories(query=args.query)
            if _matches_filters(result.record, kind=args.kind, tags=args.tags)
        ][: args.limit]
        payload = [_record_payload(record) for record in results]
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Found {len(payload)} memories",
            data={"results": payload, "count": len(payload)},
        )


@final
class MemoryListTool(_MemoryToolBase):
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="memory_list",
        description="List recent workspace-scoped runtime memories.",
        input_schema={
            "limit": {"type": "integer"},
            "kind": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _MemoryListArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

        memories = [
            record
            for record in self._resolve_memory().list_memories()
            if _matches_filters(record, kind=args.kind, tags=args.tags)
        ][: args.limit]
        payload = [_record_payload(record) for record in memories]
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Listed {len(payload)} memories",
            data={"memories": payload, "count": len(payload)},
        )


@final
class MemoryDeleteTool(_MemoryToolBase):
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="memory_delete",
        description="Delete a workspace-scoped runtime memory by id.",
        input_schema={"id": {"type": "string"}},
        read_only=False,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _MemoryDeleteArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise self._validation_error(exc) from exc

        try:
            record = self._resolve_memory().delete_memory(args.id)
        except ValueError as exc:
            raise ValueError(f"unknown memory id: {args.id}") from exc
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Deleted memory: {record.id}",
            data={"id": record.id, "deleted": True, "tombstoned": True},
        )
