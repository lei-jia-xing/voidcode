"""Content-addressed artifacts, continuation results, and execution receipts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import TypeAdapter

from ..json_values import json_wire_object, own_json_object
from .budgets import BudgetExhaustion

if TYPE_CHECKING:
    from ..hooks import StopCause
    from ..tools import ExecutionObservation, ToolResult


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Immutable artifact identity and stored metadata; ``uri`` is derived, not persisted."""

    sha256: str
    bytes: int
    media_type: str = "application/octet-stream"

    @property
    def uri(self) -> str:
        return f"artifact://{self.sha256}"

    def as_payload(self) -> dict[str, object]:
        return {"sha256": self.sha256, "bytes": self.bytes, "media_type": self.media_type}


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    """One bounded read of an artifact: `data` at `offset`, `eof` when the store ends there."""

    artifact: ArtifactReference
    offset: int
    data: bytes
    eof: bool

    @property
    def text(self) -> str:
        """The page decoded UTF-8 (invalid bytes replaced) for callers that page text."""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ContinuationResult:
    continuation_id: str
    owner: str
    session_id: str
    status: Literal["pending", "claimed", "completed", "failed", "cancelled"]
    payload: Mapping[str, object]
    run_status: str | None = None
    run_id: str | None = None
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None
    budget_exhaustion: BudgetExhaustion | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))

    def as_payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "continuation_id": self.continuation_id,
            "owner": self.owner,
            "session_id": self.session_id,
            "status": self.status,
            "payload": json_wire_object(self.payload),
        }
        if self.run_status is not None:
            result["run_status"] = self.run_status
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.output is not None:
            result["output"] = self.output
        if self.error is not None:
            result["error"] = self.error
        if self.stop_cause is not None:
            result["stop_cause"] = self.stop_cause
        if self.budget_exhaustion is not None:
            result["budget_exhaustion"] = self.budget_exhaustion.as_payload()
        return result

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ContinuationResult:
        if not isinstance(payload, Mapping):
            raise ValueError("continuation result payload must be an object")
        return _CONTINUATION_ADAPTER.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


_CONTINUATION_ADAPTER: TypeAdapter[ContinuationResult] = TypeAdapter(ContinuationResult)


@dataclass(frozen=True, slots=True)
class ToolInvocationReceipt:
    """A same-session request staged with the caller's result, never executed inside its callback.

    The core runs it after the callback returns, through ordinary admission and approval.
    Its committed result is available in this session's events under `request_id`.
    """

    request_id: str
    tool_name: str
    depth: int

    def as_payload(self) -> dict[str, object]:
        return {"request_id": self.request_id, "tool_name": self.tool_name, "depth": self.depth}


@dataclass(frozen=True, slots=True)
class ToolAwaitResult:
    """One exact background tool request's durable terminal outcome.

    A request cancelled before execution or lost with its in-memory driver has no
    `ToolResult`; the host never invents a success/failure for work that did not run.
    """

    request_id: str
    owner: str
    action_id: str
    session_id: str
    revision_id: str
    tool_name: str
    status: Literal["completed", "interrupted", "cancelled", "core_lost", "budget_exhausted"]
    result: ToolResult | None
    terminal_sequence: int
    stop_cause: StopCause | None = None
    budget_exhaustion: BudgetExhaustion | None = None
    execution: ExecutionObservation | None = None

    def __post_init__(self) -> None:
        for name in ("request_id", "owner", "action_id", "session_id", "revision_id", "tool_name"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"tool await result requires a non-empty {name}")
        if self.status not in ("completed", "interrupted", "cancelled", "core_lost", "budget_exhausted"):
            raise ValueError(f"unknown tool await terminal status {self.status!r}")
        if type(self.terminal_sequence) is not int or self.terminal_sequence < 1:
            raise ValueError("tool await terminal sequence must be a positive integer")
        if self.status == "completed" and self.result is None:
            raise ValueError("completed tool await must carry its actual result")
        if self.status == "budget_exhausted" and self.budget_exhaustion is None:
            raise ValueError("budget-exhausted tool await must carry its typed exhaustion")

    def as_payload(self) -> dict[str, object]:
        from ..codec import encode_tool_result

        return {
            "request_id": self.request_id,
            "owner": self.owner,
            "action_id": self.action_id,
            "session_id": self.session_id,
            "revision_id": self.revision_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "result": None if self.result is None else encode_tool_result(self.result),
            "terminal_sequence": self.terminal_sequence,
            "stop_cause": self.stop_cause,
            "budget_exhaustion": None
            if self.budget_exhaustion is None
            else {
                "dimension": self.budget_exhaustion.dimension,
                "source": self.budget_exhaustion.source,
                "limit": self.budget_exhaustion.limit,
                "consumed": self.budget_exhaustion.consumed,
                "remaining": self.budget_exhaustion.remaining,
                "continue_with": self.budget_exhaustion.continue_with,
            },
            "execution": None
            if self.execution is None
            else {
                "cancellation_signalled": self.execution.cancellation_signalled,
                "execution_stopped": self.execution.execution_stopped,
                "side_effect_state": self.execution.side_effect_state,
            },
        }


@dataclass(frozen=True, slots=True)
class BackgroundActionStatus:
    """Durable status of one owner background action: its state and terminal error, if any."""

    action_id: str
    status: str
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("background action status needs a non-empty action id")
        if self.status not in ("pending", "running", "interrupted", "completed", "failed", "cancelled"):
            raise ValueError(f"background action status must be a known state, got {self.status!r}")

    def as_payload(self) -> dict[str, object]:
        return {"action_id": self.action_id, "status": self.status, "error": self.error}
