from __future__ import annotations

import sys
from pathlib import Path

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.executor import HookExecutionOutcome, HookExecutionRequest, run_tool_hooks


def test_run_tool_hooks_executes_configured_pre_commands_and_reports_success(
    tmp_path: Path,
) -> None:
    hooks = RuntimeHooksConfig(
        enabled=True,
        pre_tool=((sys.executable, "-c", "print('pre ok')"),),
    )

    outcome: HookExecutionOutcome = run_tool_hooks(
        HookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            tool_name="write_file",
            phase="pre",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=7,
        )
    )

    assert outcome.failed_error is None
    assert outcome.last_sequence == 8
    assert len(outcome.events) == 1
    event = outcome.events[0]
    assert event.sequence == 8
    assert event.event_type == "runtime.tool_hook_pre"
    assert event.payload == {
        "phase": "pre",
        "tool_name": "write_file",
        "session_id": "hook-session",
        "status": "ok",
    }


def test_runtime_hooks_config_exposes_async_lifecycle_surfaces() -> None:
    hooks = RuntimeHooksConfig(
        on_session_start=(("python", "scripts/session_start.py"),),
        on_session_end=(("python", "scripts/session_end.py"),),
        on_session_idle=(("python", "scripts/session_idle.py"),),
        on_background_task_completed=(("python", "scripts/task_completed.py"),),
        on_background_task_failed=(("python", "scripts/task_failed.py"),),
        on_background_task_cancelled=(("python", "scripts/task_cancelled.py"),),
        on_delegated_result_available=(("python", "scripts/delegated_result.py"),),
    )

    assert hooks.commands_for_surface("session_start") == (("python", "scripts/session_start.py"),)
    assert hooks.commands_for_surface("session_end") == (("python", "scripts/session_end.py"),)
    assert hooks.commands_for_surface("session_idle") == (("python", "scripts/session_idle.py"),)
    assert hooks.commands_for_surface("background_task_completed") == (
        ("python", "scripts/task_completed.py"),
    )
    assert hooks.commands_for_surface("background_task_failed") == (
        ("python", "scripts/task_failed.py"),
    )
    assert hooks.commands_for_surface("background_task_cancelled") == (
        ("python", "scripts/task_cancelled.py"),
    )
    assert hooks.commands_for_surface("delegated_result_available") == (
        ("python", "scripts/delegated_result.py"),
    )
