"""Goal commands: inspect, set, edit, pause, resume, clear, and load objective."""

from __future__ import annotations

import time
from dataclasses import replace
from uuid import uuid4

from voidcode.sdk.commands import (
    CommandDefinition,
    CommandInvocation,
    CommandPresentation,
    CommandResult,
    ConfirmationRequest,
)
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

from .budget import _apply_budget, _get_closed_window_spend
from .continuations import _enqueue_pursuit
from .objective import read_full_objective, spill_objective_if_needed
from .state import ActiveWindow, GoalState, _store
from .views import GoalFormView


def _goal_presentation(goal: GoalState) -> CommandPresentation:
    objective = goal.objective[:1800]
    if len(goal.objective) > len(objective) or goal.objective_file_ref is not None:
        objective += "\n\nObjective preview; open Edit goal to load the full objective."
    if goal.token_budget is None:
        remaining = "Unbounded"
    elif goal.tokens_used is None:
        remaining = "Unknown"
    else:
        remaining = str(goal.remaining_tokens)
    lines = [
        f"Status: {goal.status}",
        objective,
        f"Token budget: {goal.token_budget if goal.token_budget is not None else 'Unbounded'}",
        f"Tokens used: {goal.tokens_used if goal.tokens_used is not None else 'Unknown'}",
        f"Remaining tokens: {remaining}",
    ]
    actions: list[dict[str, object]] = [{"label": "Edit goal", "action": "goal.edit", "prepare_input": True}]
    if goal.status in {"active", "blocked", "usage-limited", "budget-limited"}:
        actions.append({"label": "Pause goal", "action": "goal.pause"})
    if goal.status in {"paused", "blocked", "usage-limited", "budget-limited"}:
        actions.append({"label": "Resume goal", "action": "goal.resume"})
    actions.append({"label": "Clear goal", "action": "goal.clear"})
    return CommandPresentation(title="Current goal", view_type="markdown", data={"content": "\n\n".join(lines), "actions": actions})


class GoalCommand:
    """Read-only query to inspect current goal status."""

    definition = CommandDefinition(
        name="goal",
        description="Show current goal status and details.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        goal, _ = _store(services).load()
        if goal is None:
            form = GoalFormView().render(invocation.context.session_id, services)
            presentation = CommandPresentation(title=form.title, view_type="form", data=form.data)
            return CommandResult(
                status="ok",
                output="No active goal",
                data={"goal": None, "remaining_tokens": None},
                presentation=presentation,
            )
        return CommandResult(
            status="ok",
            output=f"Goal ({goal.status}): {goal.objective}",
            data={"goal": goal.to_dict(), "remaining_tokens": goal.remaining_tokens},
            presentation=_goal_presentation(goal),
        )


class GoalSetCommand:
    """Set or replace the current session goal."""

    definition = CommandDefinition(
        name="goal.set",
        text_argument="objective",
        description="Create a new goal or replace existing goal.",
        argument_schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The goal objective text."},
                "token_budget": {"type": "integer", "description": "Optional positive integer token budget."},
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = (invocation, services)
        return CommandPresentation(
            title="Set goal",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {"name": "objective", "label": "Objective", "field_type": "multiline", "required": True},
                    {"name": "token_budget", "label": "Token Budget", "field_type": "integer", "required": False},
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        existing, version = _store(services).load()

        # Replacement of unfinished goal requires user confirmation
        if existing is not None and existing.status != "complete":
            if invocation.context.actor != "user":
                req_id = uuid4().hex
                return CommandResult(
                    status="pending_confirmation",
                    confirmation=ConfirmationRequest(
                        request_id=req_id,
                        owner="goal",
                        command_name=self.definition.name,
                        arguments=invocation.arguments,
                        ui_type="confirmation",
                        ui_data={
                            "request_id": req_id,
                            "message": "An unfinished goal is in progress. Replace it with the new goal?",
                            "actions": ["allow", "deny"],
                        },
                        expected_state_version=version,
                    ),
                )

        raw_obj = invocation.arguments.get("objective")
        if not isinstance(raw_obj, str) or not raw_obj.strip():
            return CommandResult(status="error", error="Goal objective must be a non-empty string")
        objective = raw_obj

        raw_budget = invocation.arguments.get("token_budget")
        token_budget: int | None = None
        if raw_budget is not None:
            if not isinstance(raw_budget, int) or isinstance(raw_budget, bool) or raw_budget <= 0:
                return CommandResult(status="error", error="Goal token_budget must be a positive integer")
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
            created_at=now,
            updated_at=now,
            objective_file_ref=file_ref,
            objective_file_sha256=sha256,
            token_budget=token_budget,
            window_allowance=token_budget,
            active_windows=(ActiveWindow(start_sequence=start_seq),),
            last_blocked_logical_turn=None,
        )

        _apply_budget(services, since_sequence=start_seq, max_total_tokens=token_budget)

        # Replacing a goal retires the previous goal's queued pursuit in the same transaction.
        _enqueue_pursuit(services, goal_id=goal_id, trigger=f"set:{invocation.context.invocation_id}")
        services.stage_event(
            "goal_created",
            {"goal_id": goal_id, "objective": prompt_obj, "token_budget": token_budget},
        )
        _store(services).save(new_goal, version)
        return CommandResult(
            status="ok",
            output=f"Goal created: {new_goal.objective}",
            data={"goal": new_goal.to_dict(), "remaining_tokens": new_goal.remaining_tokens},
            presentation=_goal_presentation(new_goal),
        )


class GoalEditCommand:
    """Edit current goal objective or budget."""

    definition = CommandDefinition(
        name="goal.edit",
        text_argument="objective",
        description="Edit current goal objective or token budget.",
        argument_schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "The updated goal objective text."},
                "token_budget": {"type": ["integer", "null"], "description": "Adjusted token budget (positive integer or null to uncap)."},
            },
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        # Existence probe only: the form renders without state, so this never parses.
        state, _ = services.get_state("session")
        if not state:
            return CommandPresentation(title="Edit goal", view_type="notice", data={"message": "No active goal. Use /goal set to create one."})
        view = GoalFormView().render(invocation.context.session_id, services)
        return CommandPresentation(title=view.title, view_type="form", data=view.data)

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        goal, version = _store(services).load()
        if goal is None:
            return CommandResult(status="error", error="No goal exists to edit")

        if "objective" not in invocation.arguments and "token_budget" not in invocation.arguments:
            # Bounded form only: the full objective is served by the READ loader
            # (goal.load_objective); a view frame is size-bounded.
            init_vals: dict[str, object] = {}
            load_action: str | None = None
            if goal.objective_file_ref is None:
                init_vals["objective"] = goal.objective
            else:
                load_action = "goal.load_objective"
            if goal.token_budget is not None:
                init_vals["token_budget"] = goal.token_budget
            return CommandResult(
                status="ok",
                output=f"Goal edit form ({goal.status})",
                data={"goal": goal.to_dict(), "initial_values": init_vals, "load_action": load_action},
                presentation=self.prepare_input(invocation, services),
            )

        now = int(time.time() * 1000)
        prompt_obj = goal.objective
        file_ref = goal.objective_file_ref
        sha256 = goal.objective_file_sha256

        objective_changed = False
        if "objective" in invocation.arguments:
            raw_obj = invocation.arguments["objective"]
            if not isinstance(raw_obj, str) or not raw_obj.strip():
                return CommandResult(status="error", error="Goal objective must be a non-empty string")
            objective = raw_obj
            objective_changed = objective != goal.objective

        new_budget = goal.token_budget
        window_allowance = goal.window_allowance
        if "token_budget" in invocation.arguments:
            raw_b = invocation.arguments["token_budget"]
            if raw_b is not None and (not isinstance(raw_b, int) or isinstance(raw_b, bool) or raw_b <= 0):
                return CommandResult(status="error", error="token_budget must be a positive integer or null")
            new_budget = raw_b

            if new_budget is None:
                # Explicit uncap: the owner's hard constraint is removed unconditionally,
                # for every status, so ordinary chat is not left blocked by a stale budget.
                services.clear_budget()
                window_allowance = None
            else:
                # Finite budget requested: verify closed-window spend is known
                closed_spend = _get_closed_window_spend(services, goal.active_windows)
                if closed_spend is None or goal.has_unknown_usage or goal.tokens_used is None:
                    return CommandResult(
                        status="error",
                        error=(
                            "Cannot set finite budget when prior closed-window usage is unknown; uncap with token_budget=null or create a new goal"
                        ),
                    )
                open_window = next((w for w in goal.active_windows if w.end_sequence is None), None)
                if open_window is None and goal.is_active:
                    return CommandResult(
                        status="error",
                        error="Cannot set finite budget on an active goal with no open accounting window; pause and resume the goal",
                    )
                remaining = new_budget - closed_spend
                if remaining <= 0:
                    return CommandResult(
                        status="error",
                        error=(
                            f"Cannot set a finite budget at or below already spent tokens "
                            f"(closed-window spend {closed_spend}, requested {new_budget}); raise the budget or create a new goal"
                        ),
                    )
                if open_window is not None:
                    if goal.window_tokens_used is None:
                        return CommandResult(
                            status="error",
                            error=(
                                "Cannot set a finite budget while current-window token usage is unknown; "
                                "uncap with token_budget=null or create a new goal"
                            ),
                        )
                    if remaining <= goal.window_tokens_used:
                        return CommandResult(
                            status="error",
                            error=(
                                f"Cannot set a finite budget that leaves no tokens for the current open window "
                                f"(window already used {goal.window_tokens_used}, remaining after closed-window spend {remaining}); "
                                "raise the budget or create a new goal"
                            ),
                        )
                    if goal.is_active:
                        # Keep the original open-window baseline and re-stage the constraint there.
                        _apply_budget(services, since_sequence=open_window.start_sequence, max_total_tokens=remaining)
                window_allowance = remaining
        if "objective" in invocation.arguments:
            prompt_obj, file_ref, sha256 = spill_objective_if_needed(services, objective)
        # Only an objective edit resets the blocked audit; a budget-only edit is not progress.
        updated_goal = replace(
            goal,
            objective=prompt_obj,
            objective_file_ref=file_ref,
            objective_file_sha256=sha256,
            token_budget=new_budget,
            window_allowance=window_allowance,
            blocked_turns_count=0 if objective_changed else goal.blocked_turns_count,
            last_blocked_logical_turn=None if objective_changed else goal.last_blocked_logical_turn,
            objective_edited=goal.objective_edited or objective_changed,
            updated_at=now,
        )

        _store(services).save(updated_goal, version)
        services.stage_event(
            "goal_edited",
            {"goal_id": goal.goal_id, "objective": prompt_obj, "token_budget": new_budget},
        )
        return CommandResult(
            status="ok",
            output=f"Goal edited: {updated_goal.objective}",
            data={"goal": updated_goal.to_dict(), "remaining_tokens": updated_goal.remaining_tokens},
            presentation=_goal_presentation(updated_goal),
        )


class GoalPauseCommand:
    """Pause the active goal."""

    definition = CommandDefinition(
        name="goal.pause",
        description="Pause the active goal.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        goal, version = _store(services).load()
        if goal is None:
            return CommandResult(status="error", error="No goal exists to pause")
        valid_pause_states = {"active", "blocked", "usage-limited", "budget-limited"}
        if goal.status not in valid_pause_states:
            return CommandResult(status="error", error=f"Cannot pause goal with status '{goal.status}'")

        usage_report = services.get_usage()
        end_seq = usage_report.through_sequence
        updated_windows = tuple(replace(w, end_sequence=end_seq) if w.end_sequence is None else w for w in goal.active_windows)
        now = int(time.time() * 1000)
        paused_goal = replace(
            goal,
            status="paused",
            active_windows=updated_windows,
            updated_at=now,
        )

        services.clear_budget()
        services.cancel_continuations()
        _store(services).save(paused_goal, version)
        services.stage_event("goal_paused", {"goal_id": goal.goal_id})
        return CommandResult(
            status="ok",
            output="Goal paused",
            data={"goal": paused_goal.to_dict(), "remaining_tokens": paused_goal.remaining_tokens},
            presentation=_goal_presentation(paused_goal),
        )


class GoalResumeCommand:
    """Resume a paused, blocked, or limited goal."""

    definition = CommandDefinition(
        name="goal.resume",
        description="Resume paused, blocked, or limited goal.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        goal, version = _store(services).load()
        if goal is None:
            return CommandResult(status="error", error="No goal exists to resume")
        if goal.status not in {"paused", "blocked", "usage-limited", "budget-limited"}:
            return CommandResult(status="error", error=f"Cannot resume goal with status '{goal.status}'")

        usage_report = services.get_usage()
        close_seq = usage_report.through_sequence
        # Resume opens new window only after closing old
        closed_windows = tuple(replace(w, end_sequence=close_seq) if w.end_sequence is None else w for w in goal.active_windows)
        now = int(time.time() * 1000)

        if goal.token_budget is None:
            # Resume unbudgeted must clear old constraint
            services.clear_budget()
            remaining_limit = None
        else:
            # When closed-window usage unknown or still pending, finite Remaining cannot be known
            closed_spend = _get_closed_window_spend(services, closed_windows)
            if closed_spend is None or goal.has_unknown_usage or goal.tokens_used is None:
                return CommandResult(
                    status="error",
                    error=(
                        "Cannot resume budgeted goal because prior usage is unknown or pending; "
                        "uncap with goal.edit token_budget=null or create a new goal"
                    ),
                )
            remaining_limit = max(0, goal.token_budget - closed_spend)
            if remaining_limit == 0:
                return CommandResult(
                    status="error",
                    error=(
                        f"Cannot resume: goal budget exhausted ({closed_spend}/{goal.token_budget} tokens used); "
                        "edit goal token_budget to raise or uncap before resuming"
                    ),
                )
            _apply_budget(services, since_sequence=close_seq, max_total_tokens=remaining_limit)
        resumed_goal = replace(
            goal,
            status="active",
            active_windows=closed_windows + (ActiveWindow(start_sequence=close_seq),),
            window_tokens_used=0,
            window_allowance=remaining_limit,
            blocked_turns_count=0,
            last_blocked_logical_turn=None,
            updated_at=now,
        )

        _enqueue_pursuit(services, goal_id=goal.goal_id, trigger=f"resume:{invocation.context.invocation_id}")
        _store(services).save(resumed_goal, version)
        services.stage_event("goal_resumed", {"goal_id": goal.goal_id})
        return CommandResult(
            status="ok",
            output=f"Goal resumed: {resumed_goal.objective}",
            data={"goal": resumed_goal.to_dict(), "remaining_tokens": resumed_goal.remaining_tokens},
            presentation=_goal_presentation(resumed_goal),
        )


class GoalClearCommand:
    """Clear the session goal directly."""

    definition = CommandDefinition(
        name="goal.clear",
        description="Clear current goal.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        goal, version = _store(services).load()
        # A missing goal can still own a stale constraint or queued pursuit; clear both.
        services.clear_budget()
        services.cancel_continuations()
        if goal is None:
            return CommandResult(
                status="ok", output="No goal to clear", data={"cleared": True}, presentation=GoalSetCommand().prepare_input(invocation, services)
            )
        goal_id = goal.goal_id

        _store(services).save(None, version)
        services.stage_event("goal_cleared", {"goal_id": goal_id})
        return CommandResult(
            status="ok", output="Goal cleared", data={"cleared": True}, presentation=GoalSetCommand().prepare_input(invocation, services)
        )


class GoalLoadObjectiveCommand:
    """Read-only action to load the full goal objective text."""

    definition = CommandDefinition(
        name="goal.load_objective",
        slash_visible=False,
        description="Retrieve full objective text for large goals.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        goal, _ = _store(services).load()
        if goal is None:
            return CommandResult(status="error", error="No goal exists")

        _ = invocation
        try:
            full_text = read_full_objective(goal, services=services)
        except Exception as exc:
            return CommandResult(status="error", error=str(exc))

        # The full objective crosses the wire exactly once, in the canonical data field.
        return CommandResult(status="ok", data={"objective": full_text})
