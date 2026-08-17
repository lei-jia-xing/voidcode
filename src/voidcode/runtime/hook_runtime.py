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
    "HOOK_RECURSION_ENV_VAR",
    "RuntimeHookOutcome",
    "hook_execution_policy_from_metadata",
    "run_lifecycle_hooks_for_session",
    "run_tool_hooks_for_session",
]
