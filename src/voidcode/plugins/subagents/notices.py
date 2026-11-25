"""Roster event staging and completion-notice acknowledgement for subagents."""

from __future__ import annotations

from collections.abc import Sequence

from voidcode.sdk.services import ChildOutcome, ChildServices, StateServices


def stage_task_spawned(services: StateServices, *, child_id: str, label: str) -> None:
    services.stage_event("task_spawned", {"task_id": child_id, "label": label, "group_id": None})


def stage_batch_spawned(services: StateServices, *, group_id: str, task_ids: Sequence[str]) -> None:
    services.stage_event("task_batch_spawned", {"parallel_group_id": group_id, "task_ids": list(task_ids)})


def _acknowledge(outcome: ChildOutcome, services: ChildServices) -> None:
    """Consume this run's completion notice atomically with the tool result that carries it."""
    if outcome.run_id is None or outcome.status in ("running", "waiting") or outcome.budget_exhaustion is not None:
        return
    services.acknowledge_child_completion(outcome.child_session_id, outcome.run_id)
