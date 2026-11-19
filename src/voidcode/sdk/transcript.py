"""Typed conversation history, independent of the core's storage vocabulary.

This is committed history, not the current model window: compaction does not erase
entries, and a provider dispatch can be visible before its result exists.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .codec import ToolResultWire
from .hooks import RequestWindow
from .icons import Icon
from .provider import ProviderMessage, ProviderTurnResult
from .tools import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class TranscriptTool:
    call: ToolCall
    result: ToolResult
    interrupted: bool
    batch_sequence: int
    deferred: bool
    icon: Icon | None = None


@dataclass(frozen=True, slots=True)
class TranscriptDispatch:
    provider_name: str
    model_name: str
    window: RequestWindow


@dataclass(frozen=True, slots=True)
class TranscriptRun:
    status: Literal["completed", "failed", "interrupted"]


@dataclass(frozen=True, slots=True)
class TranscriptInterrupted:
    """A dispatch ended without a usable provider result; usage remains unknown."""


@dataclass(frozen=True, slots=True)
class TranscriptChild:
    child_session_id: str


type TranscriptContent = (
    ProviderMessage | ProviderTurnResult | TranscriptTool | TranscriptDispatch | TranscriptRun | TranscriptInterrupted | TranscriptChild
)


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    sequence: int
    run_id: str
    revision_id: str
    source: str
    content: TranscriptContent
    dispatch_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    """A page over source events, including bookkeeping without transcript entries.

    Always advance to ``next_sequence`` while ``has_more`` is true, even when
    ``entries`` is empty. ``source_events`` counts scanned source records, not
    projected messages. Pages never clip a message or tool result.
    """

    entries: tuple[TranscriptEntry, ...]
    next_sequence: int
    has_more: bool
    source_events: int


@dataclass(frozen=True, slots=True)
class ContextRecord:
    """One source record with core-measured size and prefix-boundary certification.

    ``entry`` is absent for bookkeeping. ``safe_boundary`` certifies the prefix
    from the session's first record through this sequence, never a page suffix.
    """

    sequence: int
    payload_bytes: int
    safe_boundary: bool
    entry: TranscriptEntry | None

    @property
    def content(self) -> TranscriptContent | None:
        return None if self.entry is None else self.entry.content


@dataclass(frozen=True, slots=True)
class ContextHistory:
    """A bounded prefix starting at the session's first source record."""

    records: tuple[ContextRecord, ...]
    reached_end: bool


class TranscriptWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TranscriptToolWire(TranscriptWireModel):
    call: ToolCall
    result: ToolResultWire
    interrupted: bool
    batch_sequence: int = Field(ge=1)
    deferred: bool
    icon: Icon | None = None


class WindowSegmentWire(TranscriptWireModel):
    role: str
    characters: int
    sha256: str
    tool_calls: int
    tool_name: str | None
    reasoning_characters: int
    image_count: int | None = None
    image_bytes: int | None = None


class RequestWindowWire(TranscriptWireModel):
    message_characters: int
    tool_schema_characters: int
    tool_count: int
    segments: list[WindowSegmentWire]


class TranscriptDispatchWire(TranscriptWireModel):
    provider_name: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    window: RequestWindowWire


class TranscriptRunWire(TranscriptWireModel):
    status: Literal["completed", "failed", "interrupted"]


class TranscriptChildWire(TranscriptWireModel):
    child_session_id: str = Field(min_length=1)


class TranscriptInterruptedWire(TranscriptWireModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TranscriptMessageWire(TranscriptWireModel):
    kind: Literal["message"] = "message"
    body: ProviderMessage


class TranscriptTurnWire(TranscriptWireModel):
    kind: Literal["turn"] = "turn"
    body: ProviderTurnResult


class TranscriptToolContentWire(TranscriptWireModel):
    kind: Literal["tool"] = "tool"
    body: TranscriptToolWire


class TranscriptDispatchContentWire(TranscriptWireModel):
    kind: Literal["dispatch"] = "dispatch"
    body: TranscriptDispatchWire


class TranscriptRunContentWire(TranscriptWireModel):
    kind: Literal["run"] = "run"
    body: TranscriptRunWire


class TranscriptInterruptedContentWire(TranscriptWireModel):
    kind: Literal["interrupted"] = "interrupted"
    body: TranscriptInterruptedWire


class TranscriptChildContentWire(TranscriptWireModel):
    kind: Literal["child"] = "child"
    body: TranscriptChildWire


TranscriptContentWire = Annotated[
    TranscriptMessageWire
    | TranscriptTurnWire
    | TranscriptToolContentWire
    | TranscriptDispatchContentWire
    | TranscriptRunContentWire
    | TranscriptInterruptedContentWire
    | TranscriptChildContentWire,
    Field(discriminator="kind"),
]


class TranscriptEntryWire(TranscriptWireModel):
    sequence: int = Field(ge=1)
    run_id: str
    revision_id: str
    source: str
    content: TranscriptContentWire
    dispatch_sequence: int | None = Field(default=None, ge=1)


class TranscriptPageWire(TranscriptWireModel):
    entries: list[TranscriptEntryWire]
    next_sequence: int
    has_more: bool
    source_events: int


class ContextRecordWire(TranscriptWireModel):
    sequence: int
    payload_bytes: int
    safe_boundary: bool
    entry: TranscriptEntryWire | None


class ContextHistoryWire(TranscriptWireModel):
    records: list[ContextRecordWire]
    reached_end: bool


_PAGE_WIRE = TypeAdapter(TranscriptPageWire)
_HISTORY_WIRE = TypeAdapter(ContextHistoryWire)
_ENTRY_WIRE = TypeAdapter(TranscriptEntryWire)
_CONTENT_WIRE = TypeAdapter(TranscriptContentWire)


def encode_transcript_content(content: TranscriptContent) -> dict[str, object]:
    """Encode one transcript content with an explicit kind discriminator.

    The discriminator is required: an untagged ``{}`` is ambiguous between
    ``TranscriptInterrupted`` and an all-default ``ProviderTurnResult``.
    """
    from .codec import encode_message, encode_provider_result, encode_tool_call, encode_tool_result

    if isinstance(content, ProviderMessage):
        return {"kind": "message", "body": encode_message(content)}
    if isinstance(content, ProviderTurnResult):
        return {"kind": "turn", "body": encode_provider_result(content)}
    if isinstance(content, TranscriptTool):
        return {
            "kind": "tool",
            "body": {
                "call": encode_tool_call(content.call),
                "result": encode_tool_result(content.result),
                "interrupted": content.interrupted,
                "batch_sequence": content.batch_sequence,
                "deferred": content.deferred,
                "icon": {"glyph": content.icon.glyph} if content.icon is not None else None,
            },
        }
    if isinstance(content, TranscriptDispatch):
        return {
            "kind": "dispatch",
            "body": {
                "provider_name": content.provider_name,
                "model_name": content.model_name,
                "window": content.window.as_payload(),
            },
        }
    if isinstance(content, TranscriptRun):
        return {"kind": "run", "body": {"status": content.status}}
    if isinstance(content, TranscriptInterrupted):
        return {"kind": "interrupted", "body": {}}
    if isinstance(content, TranscriptChild):
        return {"kind": "child", "body": {"child_session_id": content.child_session_id}}
    raise ValueError(f"unsupported transcript content: {type(content).__name__}")


def _tool_wire_to_domain(tool: TranscriptToolWire) -> TranscriptTool:
    from .codec import decode_tool_result

    if not tool.call.tool_name:
        raise ValueError("tool_name must be nonempty")
    return TranscriptTool(
        tool.call,
        decode_tool_result(tool.result.model_dump()),
        interrupted=tool.interrupted,
        batch_sequence=tool.batch_sequence,
        deferred=tool.deferred,
        icon=tool.icon,
    )


def _dispatch_wire_to_domain(dispatch: TranscriptDispatchWire) -> TranscriptDispatch:
    return TranscriptDispatch(
        dispatch.provider_name,
        dispatch.model_name,
        RequestWindow.from_payload(dispatch.window.model_dump()),
    )


def decode_transcript_content(payload: Mapping[str, object]) -> TranscriptContent:
    """Decode one discriminated transcript content; unknown kinds fail visibly."""
    return _content_wire_to_domain(_decode_json(_CONTENT_WIRE, payload, "transcript content"))


def encode_transcript_entry(entry: TranscriptEntry) -> dict[str, object]:
    return {
        "sequence": entry.sequence,
        "run_id": entry.run_id,
        "revision_id": entry.revision_id,
        "source": entry.source,
        "content": encode_transcript_content(entry.content),
        "dispatch_sequence": entry.dispatch_sequence,
    }


def _content_wire_to_domain(
    wire: (
        TranscriptMessageWire
        | TranscriptTurnWire
        | TranscriptToolContentWire
        | TranscriptDispatchContentWire
        | TranscriptRunContentWire
        | TranscriptInterruptedContentWire
        | TranscriptChildContentWire
    ),
) -> TranscriptContent:
    if isinstance(wire, TranscriptMessageWire):
        return wire.body
    if isinstance(wire, TranscriptTurnWire):
        return wire.body
    if isinstance(wire, TranscriptToolContentWire):
        return _tool_wire_to_domain(wire.body)
    if isinstance(wire, TranscriptDispatchContentWire):
        return _dispatch_wire_to_domain(wire.body)
    if isinstance(wire, TranscriptRunContentWire):
        return TranscriptRun(wire.body.status)
    if isinstance(wire, TranscriptInterruptedContentWire):
        return TranscriptInterrupted()
    return TranscriptChild(wire.body.child_session_id)


def _decode_json[T](adapter: TypeAdapter[T], payload: Mapping[str, object], label: str) -> T:
    import json

    from .json_values import json_wire_object

    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be an object")
    return adapter.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


def decode_transcript_entry(payload: Mapping[str, object]) -> TranscriptEntry:
    wire = _decode_json(_ENTRY_WIRE, payload, "transcript entry")
    return TranscriptEntry(
        wire.sequence,
        wire.run_id,
        wire.revision_id,
        wire.source,
        _content_wire_to_domain(wire.content),
        wire.dispatch_sequence,
    )


def encode_transcript_page(page: TranscriptPage) -> dict[str, object]:
    return {
        "entries": [encode_transcript_entry(entry) for entry in page.entries],
        "next_sequence": page.next_sequence,
        "has_more": page.has_more,
        "source_events": page.source_events,
    }


def decode_transcript_page(payload: Mapping[str, object]) -> TranscriptPage:
    wire = _decode_json(_PAGE_WIRE, payload, "transcript page")
    return TranscriptPage(
        tuple(
            TranscriptEntry(
                item.sequence,
                item.run_id,
                item.revision_id,
                item.source,
                _content_wire_to_domain(item.content),
                item.dispatch_sequence,
            )
            for item in wire.entries
        ),
        wire.next_sequence,
        wire.has_more,
        wire.source_events,
    )


def encode_context_history(history: ContextHistory) -> dict[str, object]:
    return {
        "records": [
            {
                "sequence": record.sequence,
                "payload_bytes": record.payload_bytes,
                "safe_boundary": record.safe_boundary,
                "entry": None if record.entry is None else encode_transcript_entry(record.entry),
            }
            for record in history.records
        ],
        "reached_end": history.reached_end,
    }


def _decode_entry_wire(item: TranscriptEntryWire) -> TranscriptEntry:
    return TranscriptEntry(
        item.sequence,
        item.run_id,
        item.revision_id,
        item.source,
        _content_wire_to_domain(item.content),
        item.dispatch_sequence,
    )


def decode_context_history(payload: Mapping[str, object]) -> ContextHistory:
    wire = _decode_json(_HISTORY_WIRE, payload, "context history")
    return ContextHistory(
        tuple(
            ContextRecord(item.sequence, item.payload_bytes, item.safe_boundary, None if item.entry is None else _decode_entry_wire(item.entry))
            for item in wire.records
        ),
        wire.reached_end,
    )
