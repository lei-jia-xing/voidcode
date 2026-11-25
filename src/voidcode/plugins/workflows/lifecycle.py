"""Owner-state deactivation transform: park live runs for resume after re-enable."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .state import WorkflowState, _state_lock


def prepare_workflows_deactivation(
    state: Mapping[str, object],
    scope: str,
    scope_key: str,
    tip_sequence: int | None,
) -> Mapping[str, object]:
    """Park every live run: the driver is gone, children stay core-owned for resume."""
    _ = (scope, scope_key, tip_sequence)
    with _state_lock(scope_key):
        decoded = WorkflowState.from_payload(state) if isinstance(state, Mapping) else WorkflowState(MappingProxyType({}))
        parked = {
            run_id: run.with_status("parked", "workflows disabled; resume after re-enable") if run.status in ("running", "paused") else run
            for run_id, run in decoded.runs.items()
        }
        return WorkflowState(MappingProxyType(parked)).to_payload()
