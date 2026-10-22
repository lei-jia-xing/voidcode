"""Typed conversation history, independent of the core's storage vocabulary.

This is committed history, not the current model window: compaction does not erase
entries, and a provider dispatch can be visible before its result exists.
"""

from dataclasses import dataclass
from typing import Literal

from .hooks import RequestWindow
from .provider import ProviderMessage, ProviderTurnResult
from .tools import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class TranscriptTool:
    call: ToolCall
    result: ToolResult
    interrupted: bool
    batch_sequence: int
    deferred: bool


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
