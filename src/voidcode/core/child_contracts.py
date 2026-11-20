"""Typed values on the in-process child-orchestration service boundary.

A plugin reaches child orchestration only through `RuntimeServices`; the values that
cross from the core's `DelegationManager`/`Engine` into that service layer are built
in-process by trusted typed code, never parsed from plugin JSON. They therefore travel
as these dataclasses: one request for a spawn, one delivery per exact result fact, and
one durable receipt identity per acknowledgment. The plugin-supplied inputs themselves
(prompt, label, allowlist, schema, keys) are still validated once in `RuntimeServices`,
the actual ingress boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..sdk.services import ChildOutcome


@dataclass(frozen=True, slots=True)
class ChildSpawnRequest:
    """One already-validated delegated-spawn request from an active service lease."""

    parent_session_id: str
    prompt: str
    label: str
    tool_allowlist: tuple[str, ...] | None
    max_steps: int | None
    readonly: bool
    notify_on_completion: bool
    owner: str
    idempotency_key: str | None
    root_action_id: str
    initial_root_budget: int | None
    admission_guard: Callable[[], bool] | None
    result_schema: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ChildWaitDelivery:
    """One undelivered child result fact, carrying its exact execution outcome."""

    outcome: ChildOutcome
    source_session_id: str
    event_sequence: int
    notice_id: str | None


@dataclass(frozen=True, slots=True)
class ChildCompletionReceipt:
    """The durable terminal-fact identity an acknowledgment stages its receipt against."""

    source_session_id: str
    event_sequence: int
    notice_id: str | None
