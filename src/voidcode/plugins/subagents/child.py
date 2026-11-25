"""Child spawn and join primitives shared by the task surfaces."""

from __future__ import annotations

from collections.abc import Mapping

from voidcode.capabilities.agent_queries import AgentDefinition, apply_agent_profile
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import ChildOutcome, ChildServices

from .constants import JOIN_SLICE_SECONDS


def _parent_stopped(context: ToolContext) -> bool:
    """Whether this invocation's parent run was cancelled; checked between side effects."""
    signal = context.abort_signal
    return signal is not None and signal.cancelled


def _spawn_child(
    services: ChildServices,
    *,
    prompt: str,
    label: str,
    max_steps: int | None,
    profile: AgentDefinition | None,
    tool_allowlist: tuple[str, ...] | None,
    notify_on_completion: bool,
    origin_readonly: bool | None,
    result_schema: Mapping[str, object] | None = None,
) -> str:
    """Apply profile text and stance; a read-only parent keeps the child read-only."""
    body, readonly, tool_allowlist, max_steps = apply_agent_profile(profile, prompt, tool_allowlist, max_steps)
    child_id = services.spawn_child(
        body,
        label=label,
        tool_allowlist=tool_allowlist,
        max_steps=max_steps,
        readonly=readonly or origin_readonly is True,
        notify_on_completion=notify_on_completion,
        result_schema=result_schema,
    )
    return child_id


def _join_child_abortable(
    services: ChildServices,
    child_id: str,
    timeout_seconds: float,
    context: ToolContext,
) -> ChildOutcome | None:
    """Join one child run in short slices; ``None`` means the parent was stopped.

    The slice bounds sum to the caller's timeout, so a parent stop is observed within one
    slice and never held for the whole default 300-second wait.
    """
    remaining = timeout_seconds
    while True:
        if _parent_stopped(context):
            return None
        slice_seconds = min(JOIN_SLICE_SECONDS, remaining)
        outcome = services.join_child(child_id, timeout_seconds=slice_seconds)
        if _parent_stopped(context):
            return None
        if outcome.status != "running" or remaining <= slice_seconds:
            return outcome
        remaining -= slice_seconds


def _is_reportable_current(outcome: ChildOutcome) -> bool:
    return outcome.run_id is not None and outcome.status not in ("running", "waiting")
