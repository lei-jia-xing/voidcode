"""Committed execution projection shared by the engine and its collaborators.

`_Cursor` is the checkpointed position inside one run, `_Execution` is the
in-memory view of that run used while it is driven, and `committed_event` is
the one strict lookup into an execution's committed event prefix. These are
plain data plus one invariant; they own no store, lock, or provider handle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..sdk.hooks import StopCause
from ..sdk.json_values import json_wire_object
from .coverage import ProjectionAllocation
from .storage.store import SessionRecord, StoredEvent


def wire_object(value: object, what: str) -> Mapping[str, object]:
    """Require a JSON object from a durable payload, failing visibly when malformed."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a JSON object")
    return json_wire_object(value)


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    batch_sequence: int | None = Field(default=None, ge=1)
    next_call: int = Field(default=0, ge=0)
    intent_sequence: int | None = Field(default=None, ge=1)
    approval_sequence: int | None = Field(default=None, ge=1)
    ask_sequence: int | None = Field(default=None, ge=1)
    ask_request_id: str | None = None
    deferred_sequence: int | None = Field(default=None, ge=1)
    deferred_depth: int = Field(default=0, ge=0)
    deferred_action_id: str | None = None
    deferred_origin_tools: tuple[str, ...] | None = None
    deferred_origin_readonly: bool | None = None
    deferred_authority_missing: bool = False
    deferred_only: bool = False
    #: Optional provider-turn cap for this logical run; independent of shared root actions.
    max_steps: int | None = None
    step: int = Field(default=0, ge=0)
    abandon_batch: bool = False
    continuation_id: str | None = None
    logical_turn: str = ""
    stop_cause: StopCause | None = None
    initial_root_budget: int | None = None
    root_id: str = ""
    #: Staged continuation this session owes itself: an activation request id or an upgrade pause id.
    resume_intent: str = ""

    @field_validator("deferred_origin_tools", mode="before")
    @classmethod
    def _decode_deferred_origin_tools(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


@dataclass(slots=True)
class _Execution:
    session: SessionRecord
    cursor: _Cursor
    events: list[StoredEvent]
    run_id: str
    lifecycle_started: bool = False
    ephemeral_steering: list[str] = field(default_factory=list)
    projection_allocation: ProjectionAllocation | None = None
    allocation_ineffective_tools: int = 0
    #: Request-scoped interaction declaration for this drive (D3): None means
    #: interactive. Unattended drives refuse ask and approval parks at the
    #: park-commit site with a typed ToolFailure and continue. Ephemeral:
    #: never checkpointed, re-declared by every run/resume entry.
    interaction: Literal["interactive", "unattended"] | None = None


def committed_event(execution: _Execution, sequence: int, kind: str) -> StoredEvent:
    """Return the exact committed event at a checkpoint reference, or fail closed."""
    if sequence < 1 or sequence > len(execution.events):
        raise ValueError("checkpoint references a missing event")
    event = execution.events[sequence - 1]
    if event.sequence != sequence or event.event_type != kind:
        raise ValueError("checkpoint references the wrong event kind")
    return event
