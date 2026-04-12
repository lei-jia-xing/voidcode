from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..runtime.events import RUNTIME_TOOL_HOOK_POST, RUNTIME_TOOL_HOOK_PRE
from .config import RuntimeHooksConfig


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
            subprocess.run(
                list(command),
                cwd=request.workspace,
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, **request.environment, request.recursion_env_var: "1"},
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            error_text = f"tool {request.phase}-hook failed for {request.tool_name}: {exc}"
            return HookExecutionOutcome(
                events=(
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


def _event_type_for_phase(phase: Literal["pre", "post"]) -> str:
    return RUNTIME_TOOL_HOOK_PRE if phase == "pre" else RUNTIME_TOOL_HOOK_POST
