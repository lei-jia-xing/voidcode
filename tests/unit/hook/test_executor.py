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
