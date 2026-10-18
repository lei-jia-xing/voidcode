"""Typed context contribution and fact observer contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .provider import ProviderTokenUsage

ObservedFactKind = Literal["usage", "invocation_finished", "run_finished"]


@dataclass(frozen=True, slots=True)
class ContextContribution:
    """Governed prompt context contribution produced by a plugin."""

    content: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("context contribution content must be a non-empty string")


@dataclass(frozen=True, slots=True)
class UsageFact:
    """Truthful provider dispatch and token usage fact."""

    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    dispatch_sequence: int
    provider_name: str
    model_name: str
    usage: ProviderTokenUsage | None


@dataclass(frozen=True, slots=True)
class InvocationFinishedFact:
    """Outcome of a completed tool or action invocation."""

    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    tool_name: str
    tool_call_id: str | None
    status: Literal["ok", "error", "cancelled"]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunFinishedFact:
    """Terminal or paused state of a governed agent run."""

    session_id: str
    run_id: str
    revision_id: str
    logical_turn: str
    event_sequence: int
    status: str
    elapsed_seconds: float | None = None


type PluginFact = UsageFact | InvocationFinishedFact | RunFinishedFact
