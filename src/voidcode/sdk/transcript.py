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


@dataclass(frozen=True, slots=True)
class TranscriptDispatch:
    provider_name: str
    model_name: str
    window: RequestWindow


@dataclass(frozen=True, slots=True)
class TranscriptRun:
    status: Literal["completed", "failed", "interrupted"]


type TranscriptContent = ProviderMessage | ProviderTurnResult | TranscriptTool | TranscriptDispatch | TranscriptRun


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    sequence: int
    run_id: str
    revision_id: str
    source: str
    content: TranscriptContent


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
