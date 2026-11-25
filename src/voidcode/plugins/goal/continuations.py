"""Goal automatic-pursuit continuation enqueueing."""

from __future__ import annotations

from voidcode.sdk.services import PluginServices


def _enqueue_pursuit(services: PluginServices, *, goal_id: str, trigger: str) -> None:
    """Replace this goal owner's pursuit queue with exactly one pursue message.

    Create/resume and a completed run can all ask for the next pursuit in the same run.
    The blanket cancel removes both pending and claimed rows (a replacement/resume may
    abort a claimed old pursuit); the following enqueue leaves one claimable message.
    The trigger names the message for durable traceability.
    """
    services.cancel_continuations()
    services.enqueue_continuation(
        {"action": "pursue_goal", "goal_id": goal_id},
        idempotency_key=f"goal:{goal_id}:pursue:{trigger}",
    )
