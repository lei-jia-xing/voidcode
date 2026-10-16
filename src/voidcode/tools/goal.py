"""Native agent tools for thread goals (Codex parity)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import ClassVar

from ..core.tool_context import ToolContext
from ..runtime.goal import MAX_THREAD_GOAL_OBJECTIVE_CHARS
from .contracts import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess


def _to_int(val: object, default: int = 0) -> int:
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return default
    return default


@dataclass(frozen=True, slots=True)
class GoalResultBody:
    goal: dict[str, object] | None
    op: str
    mutated: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "goal": self.goal,
            "op": self.op,
            "mutated": self.mutated,
        }


class GetGoalTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="get_goal",
        description=("Get the current goal for this thread, including status, budgets, token and elapsed-time usage, and remaining token budget."),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.SESSION}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        _ = call
        goal = context.goal
        remaining_tokens: int | None = None
        if goal is not None and goal.get("token_budget") is not None:
            remaining_tokens = max(0, _to_int(goal.get("token_budget")) - _to_int(goal.get("tokens_used")))

        payload: dict[str, object] = {
            "goal": (
                {
                    "threadId": goal.get("thread_id"),
                    "objective": goal.get("objective"),
                    "status": goal.get("status"),
                    "tokenBudget": goal.get("token_budget"),
                    "tokensUsed": goal.get("tokens_used", 0),
                    "timeUsedSeconds": goal.get("time_used_seconds", 0),
                    "createdAt": goal.get("created_at", 0),
                    "updatedAt": goal.get("updated_at", 0),
                }
                if goal is not None
                else None
            ),
            "remainingTokens": remaining_tokens,
        }
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps(payload, indent=2)),
            body=GoalResultBody(goal=dict(goal) if goal is not None else None, op="get", mutated=False),
        )


class CreateGoalTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="create_goal",
        description=(
            "Create a goal only when explicitly requested by the user or system/developer instructions; "
            "do not infer goals from ordinary tasks. Set token_budget only when an explicit token budget "
            "is requested. Fails if an unfinished goal exists; use update_goal only for status."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": (
                        "Required. The concrete objective to start pursuing. This starts a new active "
                        "goal when no goal exists or replaces the current goal when it is complete."
                    ),
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Positive token budget for the new goal. Omit unless explicitly requested.",
                },
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if context.goal is not None and context.goal.get("status") != "complete":
            return ToolFailure(
                tool_name=self.definition.name,
                error="cannot create a new goal because this thread has an unfinished goal; complete the existing goal first",
                output=TextOutput("cannot create a new goal because this thread has an unfinished goal; complete the existing goal first"),
            )

        args = call.arguments
        raw_objective = args.get("objective")
        if not isinstance(raw_objective, str) or not raw_objective.strip():
            return ToolFailure(
                tool_name=self.definition.name,
                error="goal objective must not be empty",
                output=TextOutput("goal objective must not be empty"),
            )
        objective = raw_objective.strip()
        if len(objective) > MAX_THREAD_GOAL_OBJECTIVE_CHARS:
            return ToolFailure(
                tool_name=self.definition.name,
                error=f"goal objective must be at most {MAX_THREAD_GOAL_OBJECTIVE_CHARS} characters",
                output=TextOutput(f"goal objective must be at most {MAX_THREAD_GOAL_OBJECTIVE_CHARS} characters"),
            )

        raw_budget = args.get("token_budget")
        token_budget: int | None = None
        if raw_budget is not None:
            if not isinstance(raw_budget, (int, float)) or int(raw_budget) <= 0:
                return ToolFailure(
                    tool_name=self.definition.name,
                    error="goal budgets must be positive when provided",
                    output=TextOutput("goal budgets must be positive when provided"),
                )
            token_budget = int(raw_budget)

        session_id = context.session_id or ""
        now = int(time.time() * 1000)
        new_goal: dict[str, object] = {
            "thread_id": session_id,
            "objective": objective,
            "status": "active",
            "token_budget": token_budget,
            "tokens_used": 0,
            "time_used_seconds": 0,
            "created_at": now,
            "updated_at": now,
        }

        payload: dict[str, object] = {
            "goal": {
                "threadId": new_goal["thread_id"],
                "objective": new_goal["objective"],
                "status": new_goal["status"],
                "tokenBudget": new_goal["token_budget"],
                "tokensUsed": new_goal["tokens_used"],
                "timeUsedSeconds": new_goal["time_used_seconds"],
                "createdAt": new_goal["created_at"],
                "updatedAt": new_goal["updated_at"],
            },
            "remainingTokens": token_budget,
        }
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps(payload, indent=2)),
            body=GoalResultBody(goal=new_goal, op="create", mutated=True),
        )


class UpdateGoalTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="update_goal",
        description=(
            "Update the existing goal. Use this tool only to mark the goal achieved or genuinely blocked. "
            "Set status to complete only when the objective has actually been achieved and no required work remains. "
            "Set status to blocked only when the same blocking condition has repeated for at least three consecutive "
            "goal turns, counting the original/user-triggered turn and any automatic continuations, and the agent "
            "cannot make meaningful progress without user input or an external-state change."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["complete", "blocked"],
                    "description": (
                        "Required. Set to complete only when the objective is achieved and no required work remains. "
                        "Set to blocked only after the same blocking condition has recurred for at least three "
                        "consecutive goal turns and the agent is at an impasse. After a previously blocked goal "
                        "is resumed, the resumed run starts a fresh blocked audit."
                    ),
                },
            },
            "required": ["status"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if context.goal is None:
            return ToolFailure(
                tool_name=self.definition.name,
                error="cannot update goal because this thread has no goal",
                output=TextOutput("cannot update goal because this thread has no goal"),
            )

        status = call.arguments.get("status")
        if status not in {"complete", "blocked"}:
            return ToolFailure(
                tool_name=self.definition.name,
                error=(
                    "update_goal can only mark the existing goal complete or blocked; pause, resume, "
                    "budget-limited, and usage-limited status changes are controlled by the user or system"
                ),
                output=TextOutput(
                    "update_goal can only mark the existing goal complete or blocked; pause, resume, "
                    "budget-limited, and usage-limited status changes are controlled by the user or system"
                ),
            )

        if status == "blocked" and context.goal_consecutive_blocked_turns < 3:
            return ToolFailure(
                tool_name=self.definition.name,
                error=(
                    f"cannot mark goal blocked before the same condition has recurred for at least 3 "
                    f"consecutive goal turns (currently {context.goal_consecutive_blocked_turns})"
                ),
                output=TextOutput(
                    f"cannot mark goal blocked before the same condition has recurred for at least 3 "
                    f"consecutive goal turns (currently {context.goal_consecutive_blocked_turns})"
                ),
            )

        now = int(time.time() * 1000)
        updated_goal = dict(context.goal)
        updated_goal["status"] = status
        updated_goal["updated_at"] = now

        remaining_tokens: int | None = None
        if updated_goal.get("token_budget") is not None:
            remaining_tokens = max(0, _to_int(updated_goal.get("token_budget")) - _to_int(updated_goal.get("tokens_used")))

        completion_budget_report: str | None = None
        if status == "complete" and updated_goal.get("token_budget") is not None:
            completion_budget_report = (
                f"Goal completed. Tokens consumed: {updated_goal.get('tokens_used', 0)} / {updated_goal['token_budget']} budget."
            )

        payload: dict[str, object] = {
            "goal": {
                "threadId": updated_goal.get("thread_id"),
                "objective": updated_goal.get("objective"),
                "status": updated_goal.get("status"),
                "tokenBudget": updated_goal.get("token_budget"),
                "tokensUsed": updated_goal.get("tokens_used", 0),
                "timeUsedSeconds": updated_goal.get("time_used_seconds", 0),
                "createdAt": updated_goal.get("created_at", 0),
                "updatedAt": updated_goal.get("updated_at", 0),
            },
            "remainingTokens": remaining_tokens,
        }
        if completion_budget_report is not None:
            payload["completionBudgetReport"] = completion_budget_report

        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps(payload, indent=2)),
            body=GoalResultBody(goal=updated_goal, op="update", mutated=True),
        )
