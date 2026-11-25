"""Goal automatic-pursuit continuation enqueueing."""

from __future__ import annotations

from voidcode.sdk.services import PluginServices


def _enqueue_pursuit(services: PluginServices, *, goal_id: str, trigger: str) -> None:
    """Queue at most one automatic pursue message for this goal.

    Create/resume and a completed run can all ask for the next pursuit in the same run.
    Replacing this owner's pending queue in the same core transaction keeps exactly one
    message claimable; the trigger names the message for durable traceability.
    """
    services.cancel_continuations()
    services.enqueue_continuation(
        {"action": "pursue_goal", "goal_id": goal_id},
        idempotency_key=f"goal:{goal_id}:pursue:{trigger}",
    )
