from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..runtime.events import (
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_CONTEXT_PRESSURE,
    RUNTIME_DELEGATED_RESULT_AVAILABLE,
    RUNTIME_SESSION_ENDED,
    RUNTIME_SESSION_IDLE,
    RUNTIME_SESSION_STARTED,
    RUNTIME_TOOL_HOOK_POST,
    RUNTIME_TOOL_HOOK_PRE,
)
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


def run_tool_hooks(request: HookExecutionRequest) -> HookExecutionOutcome:
    hooks = request.hooks
    if hooks is None or hooks.enabled is not True:
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)
    if request.environment.get(request.recursion_env_var) == "1":
        return HookExecutionOutcome(events=(), last_sequence=request.sequence_start)

    commands = hooks.pre_tool if request.phase == "pre" else hooks.post_tool
    last_sequence = request.sequence_start
    events: list[HookExecutionEvent] = []
    for command in commands:
        last_sequence += 1
        try:
            _run_hook_command(
                command=command,
                workspace=request.workspace,
                environment={**request.environment, request.recursion_env_var: "1"},
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

        events.append(
            HookExecutionEvent(
                sequence=last_sequence,
                event_type=_event_type_for_phase(request.phase),
                payload={
                    "phase": request.phase,
                    "tool_name": request.tool_name,
                    "session_id": request.session_id,
                    "status": "ok",
                },
            )
        )

    return HookExecutionOutcome(events=tuple(events), last_sequence=last_sequence)


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
    base_payload = {
        "surface": request.surface,
        "session_id": request.session_id,
        **dict(request.payload),
        **({"kind": "hook_result"} if request.surface == "context_pressure" else {}),
    }
    for command in commands:
        last_sequence += 1
        try:
            _run_hook_command(
                command=command,
                workspace=request.workspace,
                environment={
                    **request.environment,
                    **_lifecycle_hook_environment(request),
                    request.recursion_env_var: "1",
                },
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

        events.append(
            HookExecutionEvent(
                sequence=last_sequence,
                event_type=_event_type_for_surface(request.surface),
                payload={**base_payload, "hook_status": "ok"},
            )
        )

    return HookExecutionOutcome(events=tuple(events), last_sequence=last_sequence)


def _event_type_for_phase(phase: Literal["pre", "post"]) -> str:
    return RUNTIME_TOOL_HOOK_PRE if phase == "pre" else RUNTIME_TOOL_HOOK_POST


def _event_type_for_surface(surface: RuntimeHookSurface) -> str:
    return {
        "session_start": RUNTIME_SESSION_STARTED,
        "session_end": RUNTIME_SESSION_ENDED,
        "session_idle": RUNTIME_SESSION_IDLE,
        "background_task_completed": RUNTIME_BACKGROUND_TASK_COMPLETED,
        "background_task_failed": RUNTIME_BACKGROUND_TASK_FAILED,
        "background_task_cancelled": RUNTIME_BACKGROUND_TASK_CANCELLED,
        "delegated_result_available": RUNTIME_DELEGATED_RESULT_AVAILABLE,
        "context_pressure": RUNTIME_CONTEXT_PRESSURE,
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


def _run_hook_command(
    *,
    command: tuple[str, ...],
    workspace: Path,
    environment: Mapping[str, str],
    timeout_seconds: float | None,
) -> None:
    subprocess.run(
        list(command),
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_seconds,
        env={**os.environ, **environment},
    )
