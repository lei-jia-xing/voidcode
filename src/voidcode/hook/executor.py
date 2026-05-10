from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from ..runtime.events import (
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED,
    RUNTIME_BACKGROUND_TASK_PROGRESS,
    RUNTIME_BACKGROUND_TASK_REGISTERED,
    RUNTIME_BACKGROUND_TASK_RESULT_READ,
    RUNTIME_BACKGROUND_TASK_STARTED,
    RUNTIME_CONTEXT_PRESSURE,
    RUNTIME_DELEGATED_RESULT_AVAILABLE,
    RUNTIME_SESSION_ENDED,
    RUNTIME_SESSION_IDLE,
    RUNTIME_SESSION_STARTED,
    RUNTIME_STUCK_DETECTED,
    RUNTIME_TOOL_HOOK_POST,
    RUNTIME_TOOL_HOOK_PRE,
    RUNTIME_TURN_PROGRESS,
)
from ..security.shell_policy import non_interactive_shell_env, resolve_shell_command_policy
from .config import RuntimeHooksConfig, RuntimeHookSurface


def _empty_payload() -> Mapping[str, object]:
    return {}


@dataclass(frozen=True, slots=True)
class HookExecutionEvent:
    sequence: int
    event_type: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class HookExecutionOutcome:
    events: tuple[HookExecutionEvent, ...]
    last_sequence: int
    failed_error: str | None = None
    action: Literal["continue", "cancel"] = "continue"
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HookExecutionPolicy:
    mode: str = "normal"
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class HookPolicyDecision:
    allowed: bool
    outcome: Literal["allowed", "denied", "skipped"]
    reason: str | None = None
    injected_env_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HookExecutionRequest:
    hooks: RuntimeHooksConfig | None
    workspace: Path
    session_id: str
    tool_name: str
    phase: Literal["pre", "post"]
    recursion_env_var: str
    environment: Mapping[str, str]
    sequence_start: int
    policy: HookExecutionPolicy = field(default_factory=HookExecutionPolicy)


@dataclass(frozen=True, slots=True)
class LifecycleHookExecutionRequest:
    hooks: RuntimeHooksConfig | None
    workspace: Path
    session_id: str
    surface: RuntimeHookSurface
    recursion_env_var: str
    environment: Mapping[str, str]
    sequence_start: int
    payload: Mapping[str, object] = field(default_factory=_empty_payload)
    policy: HookExecutionPolicy = field(default_factory=HookExecutionPolicy)


def run_tool_hooks(request: HookExecutionRequest) -> HookExecutionOutcome:
    hooks = request.hooks
    if hooks is None or hooks.enabled is not True:
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)
    if request.environment.get(request.recursion_env_var) == "1":
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)

    commands = hooks.pre_tool if request.phase == "pre" else hooks.post_tool
    last_sequence = request.sequence_start
    events: list[HookExecutionEvent] = []
    diagnostics: list[str] = []
    for command in commands:
        last_sequence += 1
        policy_decision = _hook_policy_decision(command, request.policy)
        if not policy_decision.allowed:
            events.append(
                HookExecutionEvent(
                    sequence=last_sequence,
                    event_type=_event_type_for_phase(request.phase),
                    payload={
                        "phase": request.phase,
                        "tool_name": request.tool_name,
                        "session_id": request.session_id,
                        "status": policy_decision.outcome,
                        "hook_policy": _hook_policy_payload(
                            policy=request.policy,
                            decision=policy_decision,
                        ),
                    },
                )
            )
            if policy_decision.outcome == "skipped":
                continue
            return HookExecutionOutcome(
                events=tuple(events),
                last_sequence=last_sequence,
                failed_error=policy_decision.reason,
                diagnostics=tuple(diagnostics),
            )
        try:
            command_result = _run_hook_command(
                command=command,
                workspace=request.workspace,
                environment={**request.environment, request.recursion_env_var: "1"},
                injected_env=non_interactive_shell_env(_hook_command_text(command)),
                timeout_seconds=hooks.timeout_seconds,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            error_text = f"tool {request.phase}-hook failed for {request.tool_name}: {exc}"
            return HookExecutionOutcome(
                events=(
                    *events,
                    HookExecutionEvent(
                        sequence=last_sequence,
                        event_type=_event_type_for_phase(request.phase),
                        payload={
                            "phase": request.phase,
                            "tool_name": request.tool_name,
                            "session_id": request.session_id,
                            "status": "error",
                            "error": error_text,
                        },
                    ),
                ),
                last_sequence=last_sequence,
                failed_error=error_text,
            )

        action_payload = _hook_action_payload_from_stdout(command_result.stdout)
        action = action_payload.action
        diagnostic = action_payload.diagnostic
        guidance = action_payload.guidance
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        events.append(
            HookExecutionEvent(
                sequence=last_sequence,
                event_type=_event_type_for_phase(request.phase),
                payload={
                    "phase": request.phase,
                    "tool_name": request.tool_name,
                    "session_id": request.session_id,
                    "status": "ok",
                    "hook_policy": _hook_policy_payload(
                        policy=request.policy,
                        decision=policy_decision,
                    ),
                    **({"action": action} if action != "continue" else {}),
                    **({"diagnostic": diagnostic} if diagnostic is not None else {}),
                    **({"guidance": guidance} if guidance is not None else {}),
                },
            )
        )
        if action == "cancel":
            return HookExecutionOutcome(
                events=tuple(events),
                last_sequence=last_sequence,
                action=action,
                diagnostics=tuple(diagnostics),
            )

    return HookExecutionOutcome(
        events=tuple(events),
        last_sequence=last_sequence,
        diagnostics=tuple(diagnostics),
    )


def run_lifecycle_hooks(request: LifecycleHookExecutionRequest) -> HookExecutionOutcome:
    hooks = request.hooks
    if hooks is None or hooks.enabled is not True:
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)
    if request.environment.get(request.recursion_env_var) == "1":
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)
    if request.surface in ("pre_tool", "post_tool"):
        msg = f"tool hook surface must use run_tool_hooks: {request.surface}"
        raise ValueError(msg)

    commands = hooks.commands_for_surface(request.surface)
    last_sequence = request.sequence_start
    events: list[HookExecutionEvent] = []
    diagnostics: list[str] = []
    base_payload = {
        "surface": request.surface,
        "session_id": request.session_id,
        **dict(request.payload),
        **({"kind": "hook_result"} if request.surface == "context_pressure" else {}),
    }
    for command in commands:
        last_sequence += 1
        policy_decision = _hook_policy_decision(command, request.policy)
        if not policy_decision.allowed:
            events.append(
                HookExecutionEvent(
                    sequence=last_sequence,
                    event_type=_event_type_for_surface(request.surface),
                    payload={
                        **base_payload,
                        "hook_status": policy_decision.outcome,
                        "hook_policy": _hook_policy_payload(
                            policy=request.policy,
                            decision=policy_decision,
                        ),
                    },
                )
            )
            if policy_decision.outcome == "skipped":
                continue
            return HookExecutionOutcome(
                events=tuple(events),
                last_sequence=last_sequence,
                failed_error=policy_decision.reason,
                diagnostics=tuple(diagnostics),
            )
        try:
            command_result = _run_hook_command(
                command=command,
                workspace=request.workspace,
                environment={
                    **request.environment,
                    **_lifecycle_hook_environment(request),
                    request.recursion_env_var: "1",
                },
                injected_env=non_interactive_shell_env(_hook_command_text(command)),
                timeout_seconds=hooks.timeout_seconds,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            error_text = f"lifecycle hook failed for {request.surface}: {exc}"
            return HookExecutionOutcome(
                events=(
                    *events,
                    HookExecutionEvent(
                        sequence=last_sequence,
                        event_type=_event_type_for_surface(request.surface),
                        payload={
                            **base_payload,
                            "hook_status": "error",
                            "error": error_text,
                        },
                    ),
                ),
                last_sequence=last_sequence,
                failed_error=error_text,
            )

        action_payload = _hook_action_payload_from_stdout(command_result.stdout)
        action = action_payload.action
        diagnostic = action_payload.diagnostic
        guidance = action_payload.guidance
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        events.append(
            HookExecutionEvent(
                sequence=last_sequence,
                event_type=_event_type_for_surface(request.surface),
                payload={
                    **base_payload,
                    "hook_status": "ok",
                    "hook_policy": _hook_policy_payload(
                        policy=request.policy,
                        decision=policy_decision,
                    ),
                    **({"action": action} if action != "continue" else {}),
                    **({"diagnostic": diagnostic} if diagnostic is not None else {}),
                    **({"guidance": guidance} if guidance is not None else {}),
                },
            )
        )
        if action == "cancel":
            return HookExecutionOutcome(
                events=tuple(events),
                last_sequence=last_sequence,
                action=action,
                diagnostics=tuple(diagnostics),
            )

    return HookExecutionOutcome(
        events=tuple(events),
        last_sequence=last_sequence,
        diagnostics=tuple(diagnostics),
    )


def _event_type_for_phase(phase: Literal["pre", "post"]) -> str:
    return RUNTIME_TOOL_HOOK_PRE if phase == "pre" else RUNTIME_TOOL_HOOK_POST


def _event_type_for_surface(surface: RuntimeHookSurface) -> str:
    return {
        "session_start": RUNTIME_SESSION_STARTED,
        "session_end": RUNTIME_SESSION_ENDED,
        "session_idle": RUNTIME_SESSION_IDLE,
        "background_task_registered": RUNTIME_BACKGROUND_TASK_REGISTERED,
        "background_task_started": RUNTIME_BACKGROUND_TASK_STARTED,
        "background_task_progress": RUNTIME_BACKGROUND_TASK_PROGRESS,
        "background_task_completed": RUNTIME_BACKGROUND_TASK_COMPLETED,
        "background_task_failed": RUNTIME_BACKGROUND_TASK_FAILED,
        "background_task_cancelled": RUNTIME_BACKGROUND_TASK_CANCELLED,
        "background_task_notification_enqueued": RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED,
        "background_task_result_read": RUNTIME_BACKGROUND_TASK_RESULT_READ,
        "delegated_result_available": RUNTIME_DELEGATED_RESULT_AVAILABLE,
        "context_pressure": RUNTIME_CONTEXT_PRESSURE,
        "turn_progress": RUNTIME_TURN_PROGRESS,
        "stuck_detected": RUNTIME_STUCK_DETECTED,
    }[surface]


def _lifecycle_hook_environment(request: LifecycleHookExecutionRequest) -> dict[str, str]:
    environment = {
        "VOIDCODE_HOOK_SURFACE": request.surface,
        "VOIDCODE_SESSION_ID": request.session_id,
        "VOIDCODE_HOOK_PAYLOAD_JSON": json.dumps(dict(request.payload), sort_keys=True),
    }
    for key, value in request.payload.items():
        if value is None:
            continue
        env_key = "VOIDCODE_" + re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")
        if env_key == "VOIDCODE_":
            continue
        environment[env_key] = str(value)
    return environment


@dataclass(frozen=True, slots=True)
class _HookActionPayload:
    action: Literal["continue", "cancel"] = "continue"
    diagnostic: str | None = None
    guidance: str | None = None


def _hook_action_payload_from_stdout(stdout: str) -> _HookActionPayload:
    text = stdout.strip()
    if not text:
        return _HookActionPayload()
    try:
        raw_payload = json.loads(text)
    except json.JSONDecodeError:
        return _HookActionPayload()
    if not isinstance(raw_payload, dict):
        return _HookActionPayload()
    payload = cast(dict[str, object], raw_payload)
    action = payload.get("action")
    diagnostic = payload.get("diagnostic") or payload.get("message")
    guidance = payload.get("guidance")
    return _HookActionPayload(
        action="cancel" if action == "cancel" else "continue",
        diagnostic=diagnostic if isinstance(diagnostic, str) and diagnostic else None,
        guidance=guidance if isinstance(guidance, str) and guidance else None,
    )


def _hook_command_text(command: tuple[str, ...]) -> str:
    return shlex.join(command)


def _hook_policy_decision(
    command: tuple[str, ...],
    policy: HookExecutionPolicy,
) -> HookPolicyDecision:
    command_text = _hook_command_text(command)
    shell_decision = resolve_shell_command_policy(
        command_text,
        read_only=policy.read_only,
        non_interactive=True,
    )
    if not shell_decision.allowed:
        return HookPolicyDecision(
            allowed=False,
            outcome="skipped" if policy.read_only else "denied",
            reason=shell_decision.reason,
            injected_env_keys=shell_decision.injected_env_keys,
        )
    if policy.read_only:
        return HookPolicyDecision(
            allowed=False,
            outcome="skipped",
            reason="read-only runtime policy skips executable hook commands",
            injected_env_keys=shell_decision.injected_env_keys,
        )
    return HookPolicyDecision(
        allowed=True,
        outcome="allowed",
        injected_env_keys=shell_decision.injected_env_keys,
    )


def _hook_policy_payload(
    *,
    policy: HookExecutionPolicy,
    decision: HookPolicyDecision,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "outcome": decision.outcome,
        "mode": policy.mode,
        "read_only": policy.read_only,
    }
    if decision.reason is not None:
        payload["reason"] = decision.reason
    if decision.injected_env_keys:
        payload["injected_env_keys"] = list(decision.injected_env_keys)
    return payload


def _run_hook_command(
    *,
    command: tuple[str, ...],
    workspace: Path,
    environment: Mapping[str, str],
    injected_env: Mapping[str, str],
    timeout_seconds: float | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_seconds,
        env={**os.environ, **injected_env, **environment},
    )
