"""Goal tools: get, create, and update the session goal."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import ClassVar
from uuid import uuid4

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .budget import _apply_budget
from .continuations import _enqueue_pursuit
from .objective import spill_objective_if_needed
from .state import ActiveWindow, GoalState, _store


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
        icon=Icon(glyph="◎"),
        description=("Get the current goal for this thread, including status, budgets, token and elapsed-time usage, and remaining token budget."),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        replay_policy="safe",
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        _ = call
        goal, _ = _store(services).load()

        payload = goal.tool_payload(context.session_id) if goal is not None else {"goal": None, "remainingTokens": None}
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps(payload, indent=2)),
            body=GoalResultBody(goal=goal.to_dict() if goal is not None else None, op="get", mutated=False),
        )


class CreateGoalTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="create_goal",
        icon=Icon(glyph="◎"),
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
        replay_policy="never",
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        existing, version = _store(services).load()

        if existing is not None and existing.status != "complete":
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
        objective = raw_objective

        raw_budget = args.get("token_budget")
        token_budget: int | None = None
        if raw_budget is not None:
            if not isinstance(raw_budget, int) or isinstance(raw_budget, bool) or raw_budget <= 0:
                return ToolFailure(
                    tool_name=self.definition.name,
                    error="goal budgets must be positive when provided",
                    output=TextOutput("goal budgets must be positive when provided"),
                )
            token_budget = raw_budget
        prompt_obj, file_ref, sha256 = spill_objective_if_needed(services, objective)

        usage_report = services.get_usage()
        start_seq = usage_report.through_sequence
        now = int(time.time() * 1000)
        goal_id = uuid4().hex

        new_goal = GoalState(
            goal_id=goal_id,
            status="active",
            objective=prompt_obj,
            objective_file_ref=file_ref,
            objective_file_sha256=sha256,
            created_at=now,
            updated_at=now,
            token_budget=token_budget,
            window_allowance=token_budget,
            active_windows=(ActiveWindow(start_sequence=start_seq),),
            last_blocked_logical_turn=None,
        )

        _apply_budget(services, since_sequence=start_seq, max_total_tokens=token_budget)

        _enqueue_pursuit(services, goal_id=goal_id, trigger=f"create:{context.invocation_id}")
        services.stage_event(
            "goal_created",
            {"goal_id": goal_id, "objective": prompt_obj, "token_budget": token_budget},
        )
        _store(services).save(new_goal, version)

        payload = new_goal.tool_payload(context.session_id)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps(payload, indent=2)),
            body=GoalResultBody(goal=new_goal.to_dict(), op="create", mutated=True),
        )


class UpdateGoalTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="update_goal",
        icon=Icon(glyph="◎"),
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
        replay_policy="never",
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        current, version = _store(services).load()
        if current is None:
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

        now = int(time.time() * 1000)

        if status == current.status:
            # Repeated identical updates are accepted idempotently; they stage no
            # duplicate terminal event and never churn the state version.
            payload = current.tool_payload(context.session_id)
            return ToolSuccess(
                tool_name=self.definition.name,
                output=TextOutput(json.dumps(payload, indent=2)),
                body=GoalResultBody(goal=current.to_dict(), op="update", mutated=False),
            )

        if status == "blocked" and current.status in {"complete", "paused"}:
            reason = f"cannot mark a {current.status} goal blocked; only an active or limited goal may be blocked"
            return ToolFailure(
                tool_name=self.definition.name,
                error=reason,
                output=TextOutput(reason),
            )

        if status == "blocked":
            current_turn = context.logical_turn
            if not current_turn:
                # Without a logical turn identity a noisy native batch would be credited as
                # several turns. Refuse to count it instead of fabricating an impasse.
                return ToolFailure(
                    tool_name=self.definition.name,
                    error=(
                        "cannot assess consecutive blocked turns: this invocation carries no logical turn "
                        "identity, so repeated calls in one batch cannot be distinguished from real turns"
                    ),
                    output=TextOutput("cannot assess consecutive blocked turns: this invocation carries no logical turn identity"),
                )
            if current_turn == current.last_blocked_logical_turn:
                candidate_count = current.blocked_turns_count
            else:
                candidate_count = current.blocked_turns_count + 1

            if candidate_count < 3:
                # Stage intentional failure audit state
                audit_state = replace(
                    current,
                    blocked_turns_count=candidate_count,
                    last_blocked_logical_turn=current_turn,
                    updated_at=now,
                )
                _store(services).save(audit_state, version)
                services.stage_event(
                    "goal_blocked_attempt",
                    {
                        "goal_id": current.goal_id,
                        "attempt_count": candidate_count,
                        "logical_turn": current_turn,
                    },
                )
                reason = (
                    "cannot mark goal blocked before the same condition has recurred for at least 3 "
                    f"consecutive goal turns (currently {candidate_count})"
                )
                return ToolFailure(
                    tool_name=self.definition.name,
                    error=reason,
                    output=TextOutput(reason),
                )

            # 3 turns verified: transition to blocked
            usage_report = services.get_usage()
            end_seq = usage_report.through_sequence
            updated_windows = [ActiveWindow(w.start_sequence, end_seq if w.end_sequence is None else w.end_sequence) for w in current.active_windows]
            new_goal = replace(
                current,
                status="blocked",
                active_windows=tuple(updated_windows),
                blocked_turns_count=candidate_count,
                last_blocked_logical_turn=current_turn,
                updated_at=now,
            )
            services.clear_budget()
            services.cancel_continuations()
            _store(services).save(new_goal, version)
            services.stage_event(
                "goal_blocked",
                {"goal_id": current.goal_id, "turns": candidate_count},
            )

        else:
            # Mark achieved: close the accounting window and release the constraint.
            usage_report = services.get_usage()
            end_seq = usage_report.through_sequence
            updated_windows = [ActiveWindow(w.start_sequence, end_seq if w.end_sequence is None else w.end_sequence) for w in current.active_windows]
            new_goal = replace(
                current,
                status="complete",
                active_windows=tuple(updated_windows),
                blocked_turns_count=0,
                last_blocked_logical_turn=None,
                updated_at=now,
            )
            services.clear_budget()
            services.cancel_continuations()
            _store(services).save(new_goal, version)
            services.stage_event(
                "goal_completed",
                {"goal_id": current.goal_id, "tokens_used": current.tokens_used},
            )

        payload = new_goal.tool_payload(context.session_id)
        if new_goal.status == "complete" and new_goal.token_budget is not None and new_goal.tokens_used is not None:
            payload["completionBudgetReport"] = f"Goal completed. Tokens consumed: {new_goal.tokens_used} / {new_goal.token_budget} budget."

        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps(payload, indent=2)),
            body=GoalResultBody(goal=new_goal.to_dict(), op="update", mutated=True),
        )
