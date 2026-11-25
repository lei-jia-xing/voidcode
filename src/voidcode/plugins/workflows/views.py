"""Read-only workflow run list and the run-start form."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.sdk.services import PluginServices
from voidcode.sdk.views import PluginViewDescriptor

from .constants import _NO_CONFIG, STATUS_NODE_LIMIT
from .state import _store


class WorkflowStatusView:
    name: str = "workflows"

    def render(self, session_id: str, services: PluginServices) -> PluginViewDescriptor:
        _ = session_id
        try:
            state = _store(services).load()[0]
        except ValueError:
            return PluginViewDescriptor(
                owner="workflows", name=self.name, title="Workflows", view_type="notice", data={"message": "State unreadable."}
            )
        if not state.runs:
            return PluginViewDescriptor(
                owner="workflows", name=self.name, title="Workflows", view_type="status", data={"label": "Workflows", "value": "No runs"}
            )
        rows = [
            {"label": f"{run.workflow} ({run.status})", "value": f"{run.run_id} · {len(run.nodes)} nodes"}
            for run in sorted(state.runs.values(), key=lambda item: item.updated_at, reverse=True)[:STATUS_NODE_LIMIT]
        ]
        return PluginViewDescriptor(owner="workflows", name=self.name, title="Workflows", view_type="fields", data={"rows": rows})


class WorkflowRunFormView:
    name: str = "workflows.run"

    def __init__(self, workflows: Mapping[str, str] = _NO_CONFIG) -> None:
        self._workflows = workflows

    def render(self, session_id: str, services: PluginServices) -> PluginViewDescriptor:
        _ = (session_id, services)
        names = sorted(self._workflows)
        return PluginViewDescriptor(
            owner="workflows",
            name=self.name,
            title="Run Workflow",
            view_type="form",
            data={
                "fields": [
                    {
                        "name": "workflow",
                        "label": "Workflow",
                        "field_type": "text",
                        "required": True,
                        "options": [{"id": name, "label": name} for name in names],
                    },
                    {"name": "task", "label": "Task", "field_type": "multiline", "required": False},
                ],
                "action": "workflow.run",
                "initial_values": {"workflow": names[0]} if names else {},
                "load_action": None,
            },
        )
