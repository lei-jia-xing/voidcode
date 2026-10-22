"""User argv hook execution: the gate/execute/record loop for the four live surfaces.

The ``{action, diagnostic, guidance}`` stdout JSON protocol, fail-closed
``pre_tool`` versus fail-open everything else, the read-only policy skip, the
32-item diagnostic cap with sentinel, and the exact error/status strings.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .hook_config import RuntimeHooksConfig, hook_tool_matches

logger = logging.getLogger(__name__)

#: Lifecycle surfaces never honor ``cancel``: session start/end are advisory
#: (diagnostic + guidance kept, loop continues). Only the tool gate/post loop
#: short-circuits on first cancel.

#: Diagnostic accumulation bound. The first 32 items are kept, then one
#: sentinel; newer items are dropped.
_MAX_DIAGNOSTICS = 32
_DIAGNOSTICS_OMITTED_SENTINEL = "[additional diagnostics omitted]"


def _append_diagnostic(diagnostics: list[str], diagnostic: str) -> None:
    if len(diagnostics) < _MAX_DIAGNOSTICS:
        diagnostics.append(diagnostic)
    elif len(diagnostics) == _MAX_DIAGNOSTICS:
        diagnostics.append(_DIAGNOSTICS_OMITTED_SENTINEL)


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
    action: Literal["continue", "cancel"] | None = "continue"
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HookExecutionPolicy:
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class HookPolicyDecision:
    allowed: bool
    outcome: Literal["allowed", "denied", "skipped"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class HookExecutionRequest:
    hooks: RuntimeHooksConfig
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
    hooks: RuntimeHooksConfig
    workspace: Path
    session_id: str
    surface: Literal["session_start", "session_end"]
    recursion_env_var: str
    environment: Mapping[str, str]
    sequence_start: int
    payload: Mapping[str, object] = field(default_factory=dict)
    policy: HookExecutionPolicy = field(default_factory=HookExecutionPolicy)


@dataclass(frozen=True, slots=True)
class _HookCommandLoop:
    """Per-surface adapter for the shared hook command gate/execute/record loop."""

    event_type: str
    base_payload: Mapping[str, object]
    status_key: str
    environment: Mapping[str, str]
    fail_closed: bool
    error_label: str
    honors_cancel: bool
    label: str


def run_tool_hooks(request: HookExecutionRequest) -> HookExecutionOutcome:
    hooks = request.hooks
    if hooks.enabled is not True:
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)
    if request.environment.get(request.recursion_env_var) == "1":
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)

    commands = hooks.pre_tool if request.phase == "pre" else hooks.post_tool
    timeout_seconds = hooks.timeout_seconds
    match_patterns = hooks.pre_tool_match if request.phase == "pre" else hooks.post_tool_match
    if not hook_tool_matches(match_patterns, request.tool_name):
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)
    return _run_hook_commands(
        _HookCommandLoop(
            event_type=_event_type_for_phase(request.phase),
            base_payload={"phase": request.phase, "tool_name": request.tool_name, "session_id": request.session_id},
            status_key="status",
            environment=request.environment,
            fail_closed=request.phase == "pre",
            error_label=f"tool {request.phase}-hook failed for {request.tool_name}",
            honors_cancel=True,
            label=request.phase,
        ),
        commands=commands,
        timeout_seconds=timeout_seconds,
        workspace=request.workspace,
        recursion_env_var=request.recursion_env_var,
        policy=request.policy,
        sequence_start=request.sequence_start,
    )


def run_lifecycle_hooks(request: LifecycleHookExecutionRequest) -> HookExecutionOutcome:
    hooks = request.hooks
    if hooks.enabled is not True:
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)
    if request.environment.get(request.recursion_env_var) == "1":
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)

    commands = hooks.on_session_start if request.surface == "session_start" else hooks.on_session_end
    timeout_seconds = hooks.timeout_seconds
    return _run_hook_commands(
        _HookCommandLoop(
            event_type="runtime.lifecycle_hook",
            base_payload={"surface": request.surface, "session_id": request.session_id, **dict(request.payload)},
            status_key="hook_status",
            environment={**request.environment, **_lifecycle_hook_environment(request)},
            fail_closed=False,
            error_label=f"lifecycle hook failed for {request.surface}",
            honors_cancel=False,
            label=request.surface,
        ),
        commands=commands,
        timeout_seconds=timeout_seconds,
        workspace=request.workspace,
        recursion_env_var=request.recursion_env_var,
        policy=request.policy,
        sequence_start=request.sequence_start,
    )


def _run_hook_commands(
    adapter: _HookCommandLoop,
    *,
    commands: tuple[tuple[str, ...], ...],
    timeout_seconds: float | None,
    workspace: Path,
    recursion_env_var: str,
    policy: HookExecutionPolicy,
    sequence_start: int,
) -> HookExecutionOutcome:
    """Gate, execute and record one surface's hook commands."""
    last_sequence = sequence_start
    events: list[HookExecutionEvent] = []
    diagnostics: list[str] = []
    for command in commands:
        last_sequence += 1
        policy_decision = _hook_policy_decision(policy)
        if not policy_decision.allowed:
            events.append(
                HookExecutionEvent(
                    sequence=last_sequence,
                    event_type=adapter.event_type,
                    payload={
                        **adapter.base_payload,
                        adapter.status_key: policy_decision.outcome,
                        "hook_policy": _hook_policy_payload(policy=policy, decision=policy_decision),
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
                workspace=workspace,
                environment={**adapter.environment, recursion_env_var: "1"},
                timeout_seconds=timeout_seconds,
            )
            action_payload = _hook_action_payload_from_stdout(command_result.stdout)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as exc:
            error_text = f"{adapter.error_label}: {exc}"
            _append_diagnostic(diagnostics, error_text)
            if adapter.fail_closed:
                return HookExecutionOutcome(
                    events=(
                        *events,
                        HookExecutionEvent(
                            sequence=last_sequence,
                            event_type=adapter.event_type,
                            payload={
                                **adapter.base_payload,
                                adapter.status_key: "error",
                                "error": error_text,
                                "diagnostic": error_text,
                                "guidance": error_text,
                            },
                        ),
                    ),
                    last_sequence=last_sequence,
                    failed_error=error_text,
                    action="cancel",
                    diagnostics=tuple(diagnostics),
                )
            return HookExecutionOutcome(
                events=(
                    *events,
                    HookExecutionEvent(
                        sequence=last_sequence,
                        event_type=adapter.event_type,
                        payload={
                            **adapter.base_payload,
                            adapter.status_key: "error",
                            "error": error_text,
                            "diagnostic": error_text,
                        },
                    ),
                ),
                last_sequence=last_sequence,
                failed_error=error_text,
                action=None,
                diagnostics=tuple(diagnostics),
            )

        action = action_payload.action
        diagnostic = action_payload.diagnostic
        guidance = action_payload.guidance
        if diagnostic is not None:
            _append_diagnostic(diagnostics, diagnostic)
        if action == "cancel" and not adapter.honors_cancel:
            logger.debug("ignoring cancel from advisory surface %s", adapter.label)
            action = "continue"
        events.append(
            HookExecutionEvent(
                sequence=last_sequence,
                event_type=adapter.event_type,
                payload={
                    **adapter.base_payload,
                    adapter.status_key: "ok",
                    "hook_policy": _hook_policy_payload(policy=policy, decision=policy_decision),
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
    return "runtime.tool_hook_pre" if phase == "pre" else "runtime.tool_hook_post"


def _lifecycle_hook_environment(request: LifecycleHookExecutionRequest) -> dict[str, str]:
    return {
        "VOIDCODE_HOOK_SURFACE": request.surface,
        "VOIDCODE_SESSION_ID": request.session_id,
        "VOIDCODE_HOOK_PAYLOAD_JSON": json.dumps(dict(request.payload), sort_keys=True),
    }


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
    except json.JSONDecodeError as exc:
        if text.startswith(("{", "[")):
            raise ValueError("hook command returned malformed JSON") from exc
        return _HookActionPayload()
    if not isinstance(raw_payload, dict):
        raise ValueError("hook JSON response must be an object")

    action = raw_payload.get("action", "continue")
    if action not in ("continue", "cancel"):
        raise ValueError("hook JSON action must be 'continue' or 'cancel'")
    diagnostic = raw_payload.get("diagnostic")
    guidance = raw_payload.get("guidance")
    if diagnostic is not None and not isinstance(diagnostic, str):
        raise ValueError("hook JSON diagnostic must be a string")
    if guidance is not None and not isinstance(guidance, str):
        raise ValueError("hook JSON guidance must be a string")
    return _HookActionPayload(
        action=action,
        diagnostic=diagnostic or None,
        guidance=guidance or None,
    )


def _hook_policy_decision(policy: HookExecutionPolicy) -> HookPolicyDecision:
    if policy.read_only:
        return HookPolicyDecision(
            allowed=False,
            outcome="skipped",
            reason="read-only runtime policy skips executable hook commands",
        )
    return HookPolicyDecision(allowed=True, outcome="allowed")


def _hook_policy_payload(
    *,
    policy: HookExecutionPolicy,
    decision: HookPolicyDecision,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "outcome": decision.outcome,
        "read_only": policy.read_only,
    }
    if decision.reason is not None:
        payload["reason"] = decision.reason
    return payload


def _run_hook_command(
    *,
    command: tuple[str, ...],
    workspace: Path,
    environment: Mapping[str, str],
    timeout_seconds: float | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_seconds,
        env={**os.environ, **environment},
    )


__all__ = [
    "HookExecutionEvent",
    "HookExecutionOutcome",
    "HookExecutionPolicy",
    "HookExecutionRequest",
    "HookPolicyDecision",
    "LifecycleHookExecutionRequest",
    "run_lifecycle_hooks",
    "run_tool_hooks",
]
