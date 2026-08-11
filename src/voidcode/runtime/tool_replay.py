"""Durable intent metadata for tool execution recovery.

This module does not attempt exactly-once external effects. It records enough
information for recovery to distinguish safe reads from potentially repeated
mutations and to surface an interrupted mutation to the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..tools.contracts import ToolCall, ToolDefinition, ToolReplayPolicy
from ..tools.output import sanitize_tool_arguments

type ToolIntentStatus = Literal["pending", "completed", "interrupted"]
type ToolRecoveryAction = Literal["replay", "interrupted", "none"]


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
            replay_policy=definition.effective_replay_policy,
        )

    def metadata_payload(self) -> dict[str, object]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "replay_policy": self.replay_policy,
            "status": self.status,
        }


def interrupted_tool_result_message(intent: ToolExecutionIntent) -> str:
    return (
        f"Tool {intent.tool_name} was interrupted before its result was persisted. "
        "The runtime did not replay it because its operation may have side effects. "
        "Re-read the current state and retry only if still necessary."
    )


def recovery_action(intent: ToolExecutionIntent) -> ToolRecoveryAction:
    if intent.status != "pending":
        return "none"
    return "replay" if intent.replay_policy == "safe" else "interrupted"


__all__ = ["ToolExecutionIntent", "ToolIntentStatus", "ToolRecoveryAction", "interrupted_tool_result_message", "recovery_action"]
