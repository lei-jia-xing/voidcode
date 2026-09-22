"""Executor conflict semantics: cancel verb table, diagnostic cap, fail-closed pre_tool.

Contracts under test (design §1/§2):
- ``cancel`` short-circuits only on honoring surfaces; advisory surfaces treat it
  as ``continue`` while keeping diagnostic + guidance and looping on.
- Diagnostics accumulate in order, bounded at 32 plus one omission sentinel.
- A crashing/denied ``pre_tool`` hook fails closed: ``cancel`` + ``failed_error``
  with the reason present in both diagnostic and guidance.
- Tool-name glob filters gate the whole tool-hook surface.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.executor import (
    HookExecutionOutcome,
    HookExecutionPolicy,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)
from voidcode.hook.plan import ResolvedHookPlan, materialize_hook_plan
from voidcode.hook.surfaces import RuntimeHookSurface

_CANCEL = json.dumps({"action": "cancel", "diagnostic": "hold", "guidance": "wait"})
_CONTINUE = json.dumps({"guidance": "noted"})


def _lifecycle(
    workspace: Path,
    surface: RuntimeHookSurface,
    *commands: tuple[str, ...],
) -> HookExecutionOutcome:
    return run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=RuntimeHooksConfig(**{f"on_{surface}": commands}),
            workspace=workspace,
            session_id="session-1",
            surface=surface,
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=0,
        )
    )


def _tool(
    workspace: Path,
    *,
    tool_name: str,
    hooks: RuntimeHooksConfig,
    phase: str = "pre",
    policy: HookExecutionPolicy | None = None,
    plan: ResolvedHookPlan | None = None,
) -> HookExecutionOutcome:
    return run_tool_hooks(
        HookExecutionRequest(
            hooks=hooks,
            workspace=workspace,
            session_id="session-1",
            tool_name=tool_name,
            phase="pre" if phase == "pre" else "post",
            recursion_env_var="VOIDCODE_RUNNING_TOOL_HOOK",
            environment={},
            sequence_start=0,
            policy=policy or HookExecutionPolicy(),
            plan=plan,
        )
    )


def _crash_command() -> tuple[str, ...]:
    return (sys.executable, "-c", "raise SystemExit(3)")


def _marker_command(marker: Path) -> tuple[str, ...]:
    return (sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')")


def test_cancel_is_ignored_on_advisory_surfaces_but_keeps_payload(tmp_path: Path) -> None:
    outcome = _lifecycle(tmp_path, "session_start", ("echo", _CANCEL))
    assert outcome.action == "continue"
    payload = outcome.events[0].payload
    assert payload["diagnostic"] == "hold"
    assert payload["guidance"] == "wait"
    assert "action" not in payload


def test_advisory_cancel_keeps_looping_and_accumulates(tmp_path: Path) -> None:
    outcome = _lifecycle(
        tmp_path,
        "session_end",
        ("echo", json.dumps({"action": "cancel", "diagnostic": "first"})),
        ("echo", json.dumps({"diagnostic": "second"})),
    )
    assert outcome.action == "continue"
    assert len(outcome.events) == 2
    assert outcome.diagnostics == ("first", "second")


def test_session_idle_cancel_is_advisory(tmp_path: Path) -> None:
    """session_idle has no settle consumer, so its cancel is ignored like session_start."""
    outcome = _lifecycle(tmp_path, "session_idle", ("echo", _CANCEL), ("echo", _CONTINUE))
    assert outcome.action == "continue"
    assert len(outcome.events) == 2
    assert outcome.diagnostics == ("hold",)


def test_cancel_is_honored_on_gate_surfaces(tmp_path: Path) -> None:
    outcome = _lifecycle(tmp_path, "turn_progress", ("echo", _CANCEL), ("echo", _CONTINUE))
    assert outcome.action == "cancel"
    # First cancel stops the loop: the second command never runs.
    assert len(outcome.events) == 1


def test_diagnostics_are_capped_with_omission_sentinel(tmp_path: Path) -> None:
    commands = tuple(("echo", json.dumps({"diagnostic": f"d{index}"})) for index in range(40))
    outcome = _lifecycle(tmp_path, "session_end", *commands)
    assert len(outcome.diagnostics) == 33
    assert outcome.diagnostics[:32] == tuple(f"d{index}" for index in range(32))
    assert outcome.diagnostics[-1] == "[additional diagnostics omitted]"


def test_pre_tool_crash_fails_closed_with_reason_in_diagnostic_and_guidance(tmp_path: Path) -> None:
    outcome = _tool(
        tmp_path,
        tool_name="write",
        hooks=RuntimeHooksConfig(pre_tool=(_crash_command(),)),
    )
    assert outcome.action == "cancel"
    assert outcome.failed_error is not None
    assert outcome.diagnostics == (outcome.failed_error,)
    payload = outcome.events[0].payload
    assert payload["diagnostic"] == outcome.failed_error
    assert payload["guidance"] == outcome.failed_error


def test_post_tool_crash_does_not_cancel(tmp_path: Path) -> None:
    outcome = _tool(
        tmp_path,
        tool_name="write",
        phase="post",
        hooks=RuntimeHooksConfig(post_tool=(_crash_command(),)),
    )
    assert outcome.action == "continue"
    assert outcome.failed_error is not None


def test_read_only_policy_denial_skips_without_cancelling(tmp_path: Path) -> None:
    outcome = _tool(
        tmp_path,
        tool_name="write",
        hooks=RuntimeHooksConfig(pre_tool=(("echo", _CONTINUE),)),
        policy=HookExecutionPolicy(read_only=True),
    )
    assert outcome.action == "continue"
    assert outcome.failed_error is None
    assert outcome.events[0].payload["status"] == "skipped"


def test_pre_tool_match_filter_gates_and_unmatched_tool_runs_no_hook(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    matched = _tool(
        tmp_path,
        tool_name="write_file",
        hooks=RuntimeHooksConfig(pre_tool=(_marker_command(marker),), pre_tool_match=("write*",)),
    )
    assert marker.read_text() == "x"
    assert matched.events

    other = tmp_path / "other"
    unmatched = _tool(
        tmp_path,
        tool_name="read",
        hooks=RuntimeHooksConfig(pre_tool=(_marker_command(other),), pre_tool_match=("write*",)),
    )
    assert unmatched.events == ()
    assert not other.exists()


def test_post_tool_match_filter_is_independent_of_pre_tool(tmp_path: Path) -> None:
    outcome = _tool(
        tmp_path,
        tool_name="write",
        phase="post",
        hooks=RuntimeHooksConfig(post_tool=(("echo", _CONTINUE),), post_tool_match=("read",)),
    )
    assert outcome.events == ()


def test_success_branch_surfaces_diagnostic_and_guidance(tmp_path: Path) -> None:
    outcome = _tool(
        tmp_path,
        tool_name="write",
        hooks=RuntimeHooksConfig(pre_tool=(("echo", json.dumps({"diagnostic": "d", "guidance": "g"})),)),
    )
    assert outcome.diagnostics == ("d",)
    payload = outcome.events[0].payload
    assert payload["diagnostic"] == "d"
    assert payload["guidance"] == "g"


def test_match_filter_gates_on_plan_present_path(tmp_path: Path) -> None:
    """Plan-present is the production shape: commands come from the plan, the
    config glob filter must still gate the whole surface."""
    marker = tmp_path / "plan-ran"
    hooks = RuntimeHooksConfig(
        enabled=True,
        pre_tool=(_marker_command(marker),),
        pre_tool_match=("write*",),
    )
    plan = materialize_hook_plan(hooks)

    unmatched = _tool(tmp_path, tool_name="read", hooks=hooks, plan=plan)
    assert unmatched.events == ()
    assert not marker.exists()

    matched = _tool(tmp_path, tool_name="write_file", hooks=hooks, plan=plan)
    assert matched.events
    assert marker.read_text() == "x"

    # Sanity: the plan really does carry the command it is being filtered for.
    assert plan.commands_for_surface("pre_tool") == (_marker_command(marker),)
