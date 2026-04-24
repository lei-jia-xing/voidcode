from __future__ import annotations

import json
import sys
from pathlib import Path

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.executor import (
    HookExecutionOutcome,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)


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


def test_run_lifecycle_hooks_executes_configured_session_command_and_reports_event(
    tmp_path: Path,
) -> None:
    hooks = RuntimeHooksConfig(
        enabled=True,
        on_session_start=((sys.executable, "-c", ""),),
    )

    outcome: HookExecutionOutcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            surface="session_start",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=11,
            payload={"prompt": "hello"},
        )
    )

    assert outcome.failed_error is None
    assert outcome.last_sequence == 12
    assert len(outcome.events) == 1
    event = outcome.events[0]
    assert event.sequence == 12
    assert event.event_type == "runtime.session_started"
    assert event.payload == {
        "surface": "session_start",
        "session_id": "hook-session",
        "prompt": "hello",
        "hook_status": "ok",
    }


def test_run_lifecycle_hooks_exposes_context_as_environment(tmp_path: Path) -> None:
    output_path = tmp_path / "hook-env.txt"
    hooks = RuntimeHooksConfig(
        enabled=True,
        on_background_task_completed=(
            (
                sys.executable,
                "-c",
                "import os, pathlib; "
                "pathlib.Path('hook-env.txt').write_text("
                "os.environ['VOIDCODE_HOOK_SURFACE'] + ':' + "
                "os.environ['VOIDCODE_BACKGROUND_TASK_ID'])",
            ),
        ),
    )

    outcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            surface="background_task_completed",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=0,
            payload={"background_task_id": "task-1"},
        )
    )

    assert outcome.failed_error is None
    assert output_path.read_text() == "background_task_completed:task-1"


def test_run_lifecycle_hooks_exposes_canonical_payload_json_environment(tmp_path: Path) -> None:
    output_path = tmp_path / "hook-payload.json"
    hooks = RuntimeHooksConfig(
        enabled=True,
        on_session_start=(
            (
                sys.executable,
                "-c",
                "import os, pathlib; "
                "pathlib.Path('hook-payload.json').write_text("
                "os.environ['VOIDCODE_HOOK_PAYLOAD_JSON'])",
            ),
        ),
    )

    outcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            surface="session_start",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=0,
            payload={"prompt": "hello", "resume": True},
        )
    )

    assert outcome.failed_error is None
    assert json.loads(output_path.read_text()) == {"prompt": "hello", "resume": True}


def test_run_lifecycle_hooks_times_out_long_running_command(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(
        enabled=True,
        timeout_seconds=0.01,
        on_session_start=((sys.executable, "-c", "import time; time.sleep(1)"),),
    )

    outcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            surface="session_start",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=2,
        )
    )

    assert outcome.failed_error is not None
    assert "lifecycle hook failed for session_start" in outcome.failed_error
    assert "timed out" in outcome.failed_error
    assert outcome.events[0].payload["hook_status"] == "error"


def test_run_lifecycle_hooks_reports_failed_command(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(
        enabled=True,
        on_session_end=((sys.executable, "-c", "raise SystemExit(7)"),),
    )

    outcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            surface="session_end",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=4,
        )
    )

    assert outcome.failed_error is not None
    assert outcome.last_sequence == 5
    assert len(outcome.events) == 1
    event = outcome.events[0]
    assert event.event_type == "runtime.session_ended"
    assert event.payload["hook_status"] == "error"
    assert "lifecycle hook failed for session_end" in str(event.payload["error"])


def test_run_lifecycle_hooks_preserves_success_events_before_later_failure(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(
        enabled=True,
        on_session_end=(
            (sys.executable, "-c", ""),
            (sys.executable, "-c", "raise SystemExit(7)"),
        ),
    )

    outcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            surface="session_end",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=4,
        )
    )

    assert outcome.failed_error is not None
    assert len(outcome.events) == 2
    assert outcome.events[0].payload["hook_status"] == "ok"
    assert outcome.events[1].payload["hook_status"] == "error"


def test_run_tool_hooks_times_out_long_running_command(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(
        enabled=True,
        timeout_seconds=0.01,
        pre_tool=((sys.executable, "-c", "import time; time.sleep(1)"),),
    )

    outcome = run_tool_hooks(
        HookExecutionRequest(
            hooks=hooks,
            workspace=tmp_path,
            session_id="hook-session",
            tool_name="write_file",
            phase="pre",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=1,
        )
    )

    assert outcome.failed_error is not None
    assert "tool pre-hook failed for write_file" in outcome.failed_error
    assert "timed out" in outcome.failed_error
    assert outcome.events[0].payload["status"] == "error"
