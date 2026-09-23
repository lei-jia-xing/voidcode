from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..hook.config import RuntimeHooksConfig, RuntimeHookSurface
from ..hook.executor import (
    HookExecutionOutcome,
    HookExecutionPolicy,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)
from ..hook.plan import hook_plan_from_session_metadata
from .context.window import BeforeCompactInput
from .contracts import RuntimeStreamChunk
from .events import EventEnvelope
from .mode import runtime_mode_from_metadata, runtime_read_only_from_metadata
from .session import SessionState

HOOK_RECURSION_ENV_VAR = "VOIDCODE_RUNNING_TOOL_HOOK"


@dataclass(frozen=True, slots=True)
class RuntimeHookOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    failed_error: str | None = None
    action: Literal["continue", "cancel"] = "continue"
    guidance: tuple[str, ...] = ()


def hook_guidance_from_outcome(outcome: HookExecutionOutcome) -> tuple[str, ...]:
    """Collect argv hook ``guidance`` strings from an executor outcome.

    Guidance travels in hook event payloads only (``outcome.diagnostics``
    carries diagnostic text, never guidance). Fail-open: any unexpected
    shape yields no items so the prompt is unchanged.
    """
    try:
        events = outcome.events
    except AttributeError:
        return ()
    collected: list[str] = []
    try:
        for event in events:
            payload = event.payload
            if not isinstance(payload, dict):
                continue
            guidance = payload.get("guidance")
            if isinstance(guidance, str) and guidance.strip():
                collected.append(guidance)
    except AttributeError, TypeError:
        return ()
    return tuple(collected)


#: Bound for hook-provided custom summaries: mirrors the context-window seam
#: preview cap (``_COMPACTION_PREVIEW_CHAR_LIMIT`` in ``context/window.py``)
#: so projector input cannot grow past existing limits.
BEFORE_COMPACT_GUIDANCE_CHAR_LIMIT = 240

#: Bound for hook-provided compaction extra context: mirrors
#: ``_MAX_HOOK_GUIDANCE_CHARS`` in ``context/prompt_assembly.py``.
BEFORE_COMPACT_EXTRA_CONTEXT_CHAR_LIMIT = 2000

#: Fallback compaction-skip reason when a cancelling hook carries no diagnostic.
BEFORE_COMPACT_DEFAULT_CANCEL_REASON = "hook cancelled compaction"


def hook_cancel_reason(outcome: RuntimeHookOutcome, *, default: str) -> str:
    """First non-empty diagnostic/guidance/reason carried by ``outcome``.

    One key order for every cancel surface; ``failed_error`` wins when the
    executor set it.
    """
    if outcome.failed_error is not None and outcome.failed_error.strip():
        return outcome.failed_error
    for chunk in outcome.chunks:
        event = chunk.event
        payload = event.payload if event is not None else None
        if not isinstance(payload, dict):
            continue
        for key in ("diagnostic", "guidance", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return default


def hook_blocked_reason(outcome: RuntimeHookOutcome, *, tool_name: str) -> str:
    """LLM-visible reason for a pre-tool hook blocking ``tool_name``."""
    return f"tool '{tool_name}' blocked: " + hook_cancel_reason(
        outcome,
        default="cancelled by pre-tool hook",
    )


def before_compact_input_from_hook_outcome(outcome: RuntimeHookOutcome) -> BeforeCompactInput | None:
    """Map one foreground hook outcome onto the ``BeforeCompactInput`` seam.

    ``cancel`` skips compaction with the hook diagnostic as reason;
    ``guidance`` last-wins into ``custom_summary`` (bounded) while every other
    non-empty item becomes projector ``extra_context`` (bounded). Executor
    errors (``failed_error``) and anything else fail open: the seam is
    untouched (``None``) so compaction proceeds as configured.
    """
    if outcome.failed_error is not None:
        return None
    if outcome.action == "cancel":
        return BeforeCompactInput(cancel=True, reason=_before_compact_cancel_reason(outcome))
    items = [guidance for guidance in outcome.guidance if guidance.strip()]
    if not items:
        return None
    # ponytail: text-only blobs appended after summary; structured retention
    # (named facts, summary prompt control) needs the compact_contribution upgrade.
    extra: list[str] = []
    remaining = BEFORE_COMPACT_EXTRA_CONTEXT_CHAR_LIMIT
    for item in items[:-1]:
        if remaining <= 0:
            break
        extra.append(item[:remaining])
        remaining -= len(extra[-1])
    return BeforeCompactInput(
        custom_summary=items[-1][:BEFORE_COMPACT_GUIDANCE_CHAR_LIMIT],
        extra_context=tuple(extra),
    )


def _before_compact_cancel_reason(outcome: RuntimeHookOutcome) -> str:
    for chunk in outcome.chunks:
        event = chunk.event
        payload = event.payload if event is not None else None
        if not isinstance(payload, dict):
            continue
        for key in ("diagnostic", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return BEFORE_COMPACT_DEFAULT_CANCEL_REASON


def hook_execution_policy_from_metadata(metadata: dict[str, object] | None) -> HookExecutionPolicy:
    mode = runtime_mode_from_metadata(metadata)
    read_only = runtime_read_only_from_metadata(metadata)
    return HookExecutionPolicy(mode=mode, read_only=read_only)


def _hook_outcome_from_execution(session: SessionState, outcome: HookExecutionOutcome) -> RuntimeHookOutcome:
    emitted_chunks = tuple(
        RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=event.sequence,
                event_type=event.event_type,
                source="runtime",
                payload=event.payload,
            ),
        )
        for event in outcome.events
    )
    return RuntimeHookOutcome(
        chunks=emitted_chunks,
        last_sequence=outcome.last_sequence,
        failed_error=outcome.failed_error,
        action=outcome.action,
        guidance=hook_guidance_from_outcome(outcome),
    )


def run_tool_hooks_for_session(
    *,
    hooks: RuntimeHooksConfig | None,
    workspace: Path,
    session: SessionState,
    tool_name: str,
    phase: Literal["pre", "post"],
    recursion_env_var: str,
    sequence: int,
    policy: HookExecutionPolicy,
) -> RuntimeHookOutcome:
    outcome: HookExecutionOutcome = run_tool_hooks(
        HookExecutionRequest(
            hooks=hooks,
            plan=hook_plan_from_session_metadata(session.metadata),
            workspace=workspace,
            session_id=session.session.id,
            tool_name=tool_name,
            phase=phase,
            recursion_env_var=recursion_env_var,
            environment=os.environ,
            sequence_start=sequence,
            policy=policy,
        )
    )
    return _hook_outcome_from_execution(session, outcome)


def run_lifecycle_hooks_for_session(
    *,
    hooks: RuntimeHooksConfig | None,
    workspace: Path,
    session: SessionState,
    surface: RuntimeHookSurface,
    recursion_env_var: str,
    sequence: int,
    payload: dict[str, object] | None = None,
    policy: HookExecutionPolicy,
) -> RuntimeHookOutcome:
    outcome: HookExecutionOutcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            plan=hook_plan_from_session_metadata(session.metadata),
            workspace=workspace,
            session_id=session.session.id,
            surface=surface,
            recursion_env_var=recursion_env_var,
            environment=os.environ,
            sequence_start=sequence,
            payload=payload or {},
            policy=policy,
        )
    )
    return _hook_outcome_from_execution(session, outcome)


__all__ = [
    "BEFORE_COMPACT_EXTRA_CONTEXT_CHAR_LIMIT",
    "BEFORE_COMPACT_GUIDANCE_CHAR_LIMIT",
    "HOOK_RECURSION_ENV_VAR",
    "RuntimeHookOutcome",
    "before_compact_input_from_hook_outcome",
    "hook_blocked_reason",
    "hook_cancel_reason",
    "hook_execution_policy_from_metadata",
    "hook_guidance_from_outcome",
    "run_lifecycle_hooks_for_session",
    "run_tool_hooks_for_session",
]
