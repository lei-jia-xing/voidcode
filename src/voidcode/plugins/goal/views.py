"""Goal declarative views: status, details, and create/edit form."""

from __future__ import annotations

from voidcode.sdk.services import PluginServices
from voidcode.sdk.views import PluginViewDescriptor

from .state import _store


class GoalStatusView:
    name: str = "goal"

    def render(
        self,
        session_id: str,
        services: PluginServices,
    ) -> PluginViewDescriptor:
        _ = session_id
        goal, _ = _store(services).load()
        if goal is None:
            return PluginViewDescriptor(
                owner="goal",
                name=self.name,
                title="Goal Status",
                view_type="status",
                data={"label": "Goal", "value": "No active goal"},
            )
        used_str = str(goal.tokens_used) if goal.tokens_used is not None else "unknown"
        return PluginViewDescriptor(
            owner="goal",
            name=self.name,
            title="Goal Status",
            view_type="status",
            data={
                "label": f"Goal ({goal.status})",
                "value": f"{goal.objective[:80]} [{used_str} tokens]",
            },
        )


class GoalDetailsView:
    name: str = "goal.details"

    def render(
        self,
        session_id: str,
        services: PluginServices,
    ) -> PluginViewDescriptor:
        _ = session_id
        goal, _ = _store(services).load()
        if goal is None:
            return PluginViewDescriptor(
                owner="goal",
                name=self.name,
                title="Goal Details",
                view_type="fields",
                data={"rows": [{"label": "Status", "value": "none"}]},
            )
        objective = goal.objective[:120]
        if len(goal.objective) > len(objective) or goal.objective_file_ref is not None:
            objective += "\n\nObjective preview; open Edit goal to load the full objective."
        rows = [
            {"label": "Status", "value": goal.status},
            {"label": "Objective", "value": objective},
            {"label": "Budget", "value": str(goal.token_budget) if goal.token_budget is not None else "unbounded"},
            {"label": "Tokens Used", "value": str(goal.tokens_used) if goal.tokens_used is not None else "unknown"},
            {"label": "Time (s)", "value": f"{goal.time_used_seconds:.1f}" if goal.time_used_seconds is not None else "unknown"},
        ]
        return PluginViewDescriptor(
            owner="goal",
            name=self.name,
            title="Goal Details",
            view_type="fields",
            data={"rows": rows},
        )


class GoalFormView:
    name: str = "goal.form"

    def render(
        self,
        session_id: str,
        services: PluginServices,
    ) -> PluginViewDescriptor:
        _ = session_id
        goal, _ = _store(services).load()

        init_vals: dict[str, object] = {}
        load_action: str | None = None
        if goal is None:
            title = "Set Goal"
            action = "goal.set"
        else:
            title = "Edit Goal"
            action = "goal.edit"
            if goal.objective_file_ref is None:
                init_vals["objective"] = goal.objective
            else:
                load_action = "goal.load_objective"
            if goal.token_budget is not None:
                init_vals["token_budget"] = goal.token_budget

        return PluginViewDescriptor(
            owner="goal",
            name=self.name,
            title=title,
            view_type="form",
            data={
                "fields": [
                    {"name": "objective", "label": "Objective", "field_type": "multiline", "required": True},
                    {"name": "token_budget", "label": "Token Budget", "field_type": "integer", "required": False},
                ],
                "action": action,
                "initial_values": init_vals,
                "load_action": load_action,
            },
        )
