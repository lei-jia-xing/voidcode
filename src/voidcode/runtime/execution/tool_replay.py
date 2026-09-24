"""Durable intent metadata for tool execution recovery.

This module does not attempt exactly-once external effects. It records enough
information for recovery to distinguish safe reads from potentially repeated
mutations and to surface an interrupted mutation to the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypeIs

from ...tools.contracts import ToolCall, ToolDefinition, ToolReplayPolicy
from ...tools.output import sanitize_tool_arguments

type ToolIntentStatus = Literal["pending", "completed", "interrupted"]
type ToolRecoveryAction = Literal["replay", "interrupted", "none"]

_TOOL_REPLAY_POLICIES: Final[tuple[ToolReplayPolicy, ...]] = ("safe", "never")
_TOOL_INTENT_STATUSES: Final[tuple[ToolIntentStatus, ...]] = ("pending", "completed", "interrupted")


def is_tool_replay_policy(value: object) -> TypeIs[ToolReplayPolicy]:
    """Whether an untrusted ``replay_policy`` token names one of the tool replay policies."""
    return value in _TOOL_REPLAY_POLICIES


def is_tool_intent_status(value: object) -> TypeIs[ToolIntentStatus]:
    """Whether an untrusted ``status`` token names one of the tool intent statuses."""
    return value in _TOOL_INTENT_STATUSES


@dataclass(frozen=True, slots=True)
class ToolExecutionIntent:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, object]
    replay_policy: ToolReplayPolicy
    status: ToolIntentStatus = "pending"

    @classmethod
    def from_call(cls, call: ToolCall, definition: ToolDefinition, *, tool_call_id: str) -> ToolExecutionIntent:
        return cls(
            tool_call_id=tool_call_id,
            tool_name=call.tool_name,
            arguments=sanitize_tool_arguments(dict(call.arguments)),
            replay_policy=definition.effective_replay_policy_for(call.arguments),
        )

    def metadata_payload(self) -> dict[str, object]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "replay_policy": self.replay_policy,
            "status": self.status,
        }


def recovery_action(intent: ToolExecutionIntent) -> ToolRecoveryAction:
    if intent.status != "pending":
        return "none"
    return "replay" if intent.replay_policy == "safe" else "interrupted"


__all__ = ["ToolExecutionIntent", "ToolIntentStatus", "ToolRecoveryAction", "recovery_action"]
