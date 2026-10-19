"""Unit tests for the hooks plugin argv executor and configuration port.

Every hook command here is a real subprocess (``sys.executable -c`` or Python's
own ``sys.exit``), so the tests exercise the actual ``subprocess.run`` path:
stdout JSON parsing, exit codes, timeouts, cancel handling and environment.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from voidcode.plugins.hooks.hook_config import (
    HOOK_PLAN_SCHEMA_VERSION,
    HookPlanBinding,
    HookPlanValidationError,
    ResolvedHookPlan,
    RuntimeHooksConfig,
    hook_tool_matches,
    materialize_hook_plan,
)
from voidcode.plugins.hooks.hook_executor import (
    HookExecutionPolicy,
    HookExecutionRequest,
    LifecycleHookExecutionRequest,
    run_lifecycle_hooks,
    run_tool_hooks,
)

RECURSION_ENV_VAR = "VOIDCODE_HOOK_RECURSION"


def _emit(payload: object) -> tuple[str, ...]:
    """argv of a real subprocess that prints ``payload`` as JSON on stdout."""
    return (sys.executable, "-c", f"import json; print(json.dumps({payload!r}))")


def _tool_request(
    workspace: Path,
    *,
    hooks: RuntimeHooksConfig | None = None,
    phase: str = "pre",
    tool_name: str = "write_file",
    sequence_start: int = 0,
    plan: ResolvedHookPlan | None = None,
    policy: HookExecutionPolicy | None = None,
    environment: dict[str, str] | None = None,
) -> HookExecutionRequest:
    return HookExecutionRequest(
        hooks=hooks,
        workspace=workspace,
        session_id="sess-1",
        tool_name=tool_name,
        phase=phase,  # type: ignore[arg-type]
        recursion_env_var=RECURSION_ENV_VAR,
        environment=environment or {},
        sequence_start=sequence_start,
        policy=policy or HookExecutionPolicy(),
        plan=plan,
    )


def _lifecycle_request(
    workspace: Path,
    *,
    hooks: RuntimeHooksConfig | None = None,
    surface: str = "session_start",
    sequence_start: int = 0,
    payload: dict[str, object] | None = None,
    policy: HookExecutionPolicy | None = None,
    plan: ResolvedHookPlan | None = None,
) -> LifecycleHookExecutionRequest:
    return LifecycleHookExecutionRequest(
        hooks=hooks,
        workspace=workspace,
        session_id="sess-1",
        surface=surface,  # type: ignore[arg-type]
        recursion_env_var=RECURSION_ENV_VAR,
        environment={},
        sequence_start=sequence_start,
        payload=payload or {},
        policy=policy or HookExecutionPolicy(),
        plan=plan,
    )


MARKER_WRITER = (
    sys.executable,
    "-c",
    "import json, pathlib; pathlib.Path('marker.txt').write_text('ran'); print(json.dumps({'action': 'continue'}))",
)
SLOW_HOOK = (sys.executable, "-c", "import time, json; time.sleep(5); print(json.dumps({'action': 'cancel'}))")


# --- stdout JSON protocol -----------------------------------------------------


def test_pre_hook_reports_diagnostic_and_guidance(tmp_path: Path) -> None:
    hook = _emit({"action": "continue", "diagnostic": "diag text", "guidance": "guidance text"})
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(hook,))))

    assert outcome.action == "continue"
    assert outcome.failed_error is None
    assert outcome.last_sequence == 1
    assert outcome.diagnostics == ("diag text",)
    (event,) = outcome.events
    assert event.sequence == 1
    assert event.event_type == "runtime.tool_hook_pre"
    assert event.payload == {
        "phase": "pre",
        "tool_name": "write_file",
        "session_id": "sess-1",
        "status": "ok",
        "hook_policy": {"outcome": "allowed", "read_only": False},
        "diagnostic": "diag text",
        "guidance": "guidance text",
    }


def test_hook_recursion_guard_env_var_is_exported(tmp_path: Path) -> None:
    hook = (
        sys.executable,
        "-c",
        "import json, os; print(json.dumps({'guidance': os.environ['VOIDCODE_HOOK_RECURSION']}))",
    )
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(hook,))))

    assert outcome.events[0].payload["guidance"] == "1"


def test_empty_and_non_json_stdout_continue_the_loop(tmp_path: Path) -> None:
    silent = (sys.executable, "-c", "pass")
    prose = (sys.executable, "-c", "print('hook ran fine')")
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(silent, prose))))

    assert outcome.action == "continue"
    assert outcome.failed_error is None
    assert outcome.diagnostics == ()
    assert [event.payload["status"] for event in outcome.events] == ["ok", "ok"]
    assert all("action" not in event.payload for event in outcome.events)


def test_empty_guidance_is_dropped_and_whitespace_is_verbatim(tmp_path: Path) -> None:
    long_guidance = "g" * 5000
    blank = _emit({"action": "continue", "diagnostic": "", "guidance": ""})
    padded = _emit({"action": "continue", "diagnostic": "  ", "guidance": "   "})
    full = _emit({"guidance": long_guidance})
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(blank, padded, full))))

    assert "guidance" not in outcome.events[0].payload
    assert "diagnostic" not in outcome.events[0].payload
    assert outcome.events[1].payload["guidance"] == "   "
    assert outcome.events[1].payload["diagnostic"] == "  "
    assert outcome.diagnostics == ("  ",)
    assert outcome.events[2].payload["guidance"] == long_guidance


@pytest.mark.parametrize(
    ("stdout_expr", "message"),
    [
        ("'{\"action\": '", "hook command returned malformed JSON"),
        ("json.dumps([1, 2])", "hook JSON response must be an object"),
        ("json.dumps({'action': 'deny'})", "hook JSON action must be 'continue' or 'cancel'"),
        ("json.dumps({'diagnostic': 5})", "hook JSON diagnostic must be a string"),
        ("json.dumps({'guidance': ['x']})", "hook JSON guidance must be a string"),
    ],
)
def test_malformed_stdout_fails_closed_on_pre_tool(tmp_path: Path, stdout_expr: str, message: str) -> None:
    hook = (sys.executable, "-c", f"import json, sys; sys.stdout.write({stdout_expr})")
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(hook,))))

    assert outcome.action == "cancel"
    assert outcome.failed_error == f"tool pre-hook failed for write_file: {message}"
    assert outcome.diagnostics == (outcome.failed_error,)
    (event,) = outcome.events
    assert event.payload["status"] == "error"
    assert event.payload["error"] == outcome.failed_error
    assert event.payload["diagnostic"] == outcome.failed_error
    assert event.payload["guidance"] == outcome.failed_error


def test_malformed_stdout_warns_on_post_tool(tmp_path: Path) -> None:
    hook = (sys.executable, "-c", "import sys; sys.stdout.write('{\"action\": ')")
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(post_tool=(hook,)), phase="post"))

    assert outcome.action is None
    assert outcome.failed_error == "tool post-hook failed for write_file: hook command returned malformed JSON"
    (event,) = outcome.events
    assert event.event_type == "runtime.tool_hook_post"
    assert event.payload["status"] == "error"
    assert "guidance" not in event.payload


# --- subprocess failure handling ---------------------------------------------


def test_pre_tool_non_zero_exit_fails_closed(tmp_path: Path) -> None:
    hook = (sys.executable, "-c", "import sys; sys.exit(3)")
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(hook,))))

    assert outcome.action == "cancel"
    assert outcome.failed_error is not None
    assert outcome.failed_error.startswith("tool pre-hook failed for write_file: ")
    assert "returned non-zero exit status 3" in outcome.failed_error
    assert outcome.events[0].payload["status"] == "error"


def test_post_tool_non_zero_exit_fails_open(tmp_path: Path) -> None:
    later = MARKER_WRITER
    hook = (sys.executable, "-c", "import sys; sys.exit(3)")
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(post_tool=(hook, later)), phase="post"))

    assert outcome.action is None
    assert outcome.failed_error is not None
    assert "returned non-zero exit status 3" in outcome.failed_error
    # fail-open returns immediately: the later command never ran.
    assert not (tmp_path / "marker.txt").exists()


def test_missing_executable_fails_closed_on_pre_tool(tmp_path: Path) -> None:
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(("voidcode-no-such-hook-binary",),))))

    assert outcome.action == "cancel"
    assert outcome.failed_error is not None
    assert outcome.failed_error.startswith("tool pre-hook failed for write_file: ")
    assert "No such file or directory" in outcome.failed_error


def test_timeout_override_fails_closed_on_pre_tool(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=(SLOW_HOOK,), timeout_seconds=0.25)
    started = time.monotonic()
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks))
    elapsed = time.monotonic() - started

    assert elapsed < 4.0
    assert outcome.action == "cancel"
    assert outcome.failed_error is not None
    assert outcome.failed_error.startswith("tool pre-hook failed for write_file: ")
    assert "timed out after 0.25 seconds" in outcome.failed_error


def test_timeout_fails_open_on_post_tool(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(post_tool=(SLOW_HOOK,), timeout_seconds=0.25)
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, phase="post"))

    assert outcome.action is None
    assert outcome.failed_error is not None
    assert "timed out after 0.25 seconds" in outcome.failed_error


def test_hook_command_cwd_is_the_workspace(tmp_path: Path) -> None:
    hook = (
        sys.executable,
        "-c",
        "import json, pathlib; pathlib.Path('marker.txt').write_text('ran'); print(json.dumps({}))",
    )
    run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(hook,))))

    assert (tmp_path / "marker.txt").read_text() == "ran"


# --- cancel semantics ---------------------------------------------------------


def test_cancel_short_circuits_pre_tool_loop(tmp_path: Path) -> None:
    cancel = _emit({"action": "cancel", "diagnostic": "blocked by policy", "guidance": "fix the command"})
    hooks = RuntimeHooksConfig(pre_tool=(cancel, MARKER_WRITER))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, sequence_start=10))

    assert outcome.action == "cancel"
    assert outcome.last_sequence == 11
    assert outcome.diagnostics == ("blocked by policy",)
    (event,) = outcome.events
    assert event.sequence == 11
    assert event.payload["action"] == "cancel"
    assert event.payload["diagnostic"] == "blocked by policy"
    assert event.payload["guidance"] == "fix the command"
    assert not (tmp_path / "marker.txt").exists()


def test_cancel_is_ignored_on_advisory_lifecycle_surface(tmp_path: Path) -> None:
    cancel = _emit({"action": "cancel", "diagnostic": "advisory", "guidance": "take it into account"})
    hooks = RuntimeHooksConfig(on_session_start=(cancel, MARKER_WRITER))
    outcome = run_lifecycle_hooks(_lifecycle_request(tmp_path, hooks=hooks))

    assert outcome.action == "continue"
    assert outcome.last_sequence == 2
    assert len(outcome.events) == 2
    assert "action" not in outcome.events[0].payload
    assert outcome.events[0].payload["diagnostic"] == "advisory"
    assert outcome.events[0].payload["guidance"] == "take it into account"
    assert outcome.diagnostics == ("advisory",)
    assert (tmp_path / "marker.txt").exists()


@pytest.mark.parametrize(
    "surface",
    ["turn_progress", "tool_batch_repeated", "approval_requested", "before_compact"],
)
def test_cancel_honoring_surfaces_stop_the_loop(tmp_path: Path, surface: str) -> None:
    hooks = RuntimeHooksConfig(**{f"on_{surface}": (_emit({"action": "cancel"}), MARKER_WRITER)})
    outcome = run_lifecycle_hooks(_lifecycle_request(tmp_path, hooks=hooks, surface=surface))

    assert outcome.action == "cancel"
    assert outcome.last_sequence == 1
    assert not (tmp_path / "marker.txt").exists()


@pytest.mark.parametrize(
    "surface",
    [
        "session_start",
        "session_end",
        "session_idle",
        "background_task_registered",
        "background_task_progress",
        "delegated_result_available",
        "question_asked",
    ],
)
def test_advisory_surfaces_keep_running_after_cancel(tmp_path: Path, surface: str) -> None:
    hooks = RuntimeHooksConfig(**{f"on_{surface}": (_emit({"action": "cancel"}), MARKER_WRITER)})
    outcome = run_lifecycle_hooks(_lifecycle_request(tmp_path, hooks=hooks, surface=surface))

    assert outcome.action == "continue"
    assert (tmp_path / "marker.txt").exists()


# --- lifecycle surface plumbing ----------------------------------------------


def test_lifecycle_hook_environment_and_payload(tmp_path: Path) -> None:
    hook = (
        sys.executable,
        "-c",
        "import json, os; print(json.dumps({'guidance': '|'.join("
        "[os.environ['VOIDCODE_HOOK_SURFACE'], os.environ['VOIDCODE_SESSION_ID'],"
        " os.environ['VOIDCODE_HOOK_PAYLOAD_JSON']])}))",
    )
    hooks = RuntimeHooksConfig(on_turn_progress=(hook,))
    outcome = run_lifecycle_hooks(_lifecycle_request(tmp_path, hooks=hooks, surface="turn_progress", payload={"tool_name": "write_file"}))

    assert outcome.events[0].payload["guidance"] == 'turn_progress|sess-1|{"tool_name": "write_file"}'
    assert outcome.events[0].event_type == "runtime.lifecycle_hook"
    assert outcome.events[0].payload["hook_status"] == "ok"
    assert outcome.events[0].payload["tool_name"] == "write_file"
    assert outcome.events[0].payload["surface"] == "turn_progress"


def test_lifecycle_surface_rejects_tool_surfaces(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tool hook surface must use run_tool_hooks: pre_tool"):
        run_lifecycle_hooks(_lifecycle_request(tmp_path, hooks=RuntimeHooksConfig(), surface="pre_tool"))


# --- gating: disabled, empty, recursion, globs, read-only --------------------


def test_empty_command_list_is_a_no_op(tmp_path: Path) -> None:
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(), sequence_start=7))

    assert outcome.events == ()
    assert outcome.last_sequence == 7
    assert outcome.failed_error is None


def test_disabled_or_absent_hooks_are_a_no_op(tmp_path: Path) -> None:
    marker = MARKER_WRITER
    disabled = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(marker,), enabled=False), sequence_start=3))
    absent = run_tool_hooks(_tool_request(tmp_path, hooks=None, sequence_start=3))

    assert (disabled.events, disabled.last_sequence) == ((), 3)
    assert (absent.events, absent.last_sequence) == ((), 3)
    assert not (tmp_path / "marker.txt").exists()


def test_recursion_guard_skips_execution(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=(MARKER_WRITER,))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, environment={RECURSION_ENV_VAR: "1"}, sequence_start=4))

    assert (outcome.events, outcome.last_sequence) == ((), 4)
    assert not (tmp_path / "marker.txt").exists()


def test_tool_name_glob_filtering(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=(MARKER_WRITER,), pre_tool_match=("write*",))

    matching = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, tool_name="write"))
    assert len(matching.events) == 1
    assert (tmp_path / "marker.txt").exists()
    (tmp_path / "marker.txt").unlink()

    non_matching = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, tool_name="read", sequence_start=5))
    assert (non_matching.events, non_matching.last_sequence) == ((), 5)
    assert not (tmp_path / "marker.txt").exists()


def test_tool_name_match_is_case_sensitive_and_phase_specific(tmp_path: Path) -> None:
    upper = RuntimeHooksConfig(pre_tool=(MARKER_WRITER,), pre_tool_match=("WRITE*",))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=upper, tool_name="write_file"))
    assert outcome.events == ()

    post_only = RuntimeHooksConfig(post_tool=(MARKER_WRITER,), post_tool_match=("read",))
    pre_phase = run_tool_hooks(_tool_request(tmp_path, hooks=post_only, phase="pre", tool_name="read"))
    assert pre_phase.events == ()
    post_phase = run_tool_hooks(_tool_request(tmp_path, hooks=post_only, phase="post", tool_name="read"))
    assert len(post_phase.events) == 1
    assert post_phase.events[0].event_type == "runtime.tool_hook_post"


def test_hook_tool_matches_empty_patterns_match_every_tool() -> None:
    assert hook_tool_matches((), "anything") is True
    assert hook_tool_matches(("write*",), "write_file") is True
    assert hook_tool_matches(("read", "grep"), "grep") is True
    assert hook_tool_matches(("read",), "write_file") is False


def test_read_only_policy_skips_commands(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=(MARKER_WRITER, MARKER_WRITER))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, policy=HookExecutionPolicy(read_only=True), sequence_start=2))

    assert outcome.failed_error is None
    assert outcome.last_sequence == 4
    assert len(outcome.events) == 2
    assert [event.payload["status"] for event in outcome.events] == ["skipped", "skipped"]
    assert outcome.events[0].payload["hook_policy"] == {
        "outcome": "skipped",
        "read_only": True,
        "reason": "read-only runtime policy skips executable hook commands",
    }
    assert not (tmp_path / "marker.txt").exists()


# --- diagnostics cap ----------------------------------------------------------


def test_diagnostics_are_capped_with_sentinel(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=tuple(_emit({"diagnostic": f"d{index}"}) for index in range(40)))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks))

    assert len(outcome.events) == 40
    assert len(outcome.diagnostics) == 33
    assert outcome.diagnostics[:32] == tuple(f"d{index}" for index in range(32))
    assert outcome.diagnostics[-1] == "[additional diagnostics omitted]"


# --- frozen plan --------------------------------------------------------------


def test_materialized_plan_orders_commands_by_surface_table(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(
        pre_tool=(MARKER_WRITER, _emit({"action": "continue"})),
        on_session_start=(MARKER_WRITER,),
        on_before_compact=(MARKER_WRITER,),
        timeout_seconds=12.5,
    )
    plan = materialize_hook_plan(hooks, plan_id="test-plan")

    assert plan.schema_version == HOOK_PLAN_SCHEMA_VERSION
    assert plan.plan_hash
    assert [binding.binding_id for binding in plan.bindings] == [
        "session:pre_tool:1",
        "session:pre_tool:2",
        "session:session_start:3",
        "session:before_compact:4",
    ]
    assert [binding.phase for binding in plan.bindings] == [
        "foreground",
        "foreground",
        "foreground",
        "foreground",
    ]
    assert plan.commands_for_surface("pre_tool") == hooks.pre_tool
    assert plan.commands_for_surface("session_start") == hooks.on_session_start
    assert plan.commands_for_surface("session_end") == ()
    assert plan.timeout_seconds == 12.5
    assert plan.enabled is True
    assert ResolvedHookPlan.from_payload(plan.to_payload()).bindings == plan.bindings

    with pytest.raises(HookPlanValidationError, match="unknown hook event/surface: nope"):
        plan.commands_for_surface("nope")


def test_plan_rejects_duplicate_binding_and_bad_order() -> None:
    with pytest.raises(HookPlanValidationError, match="duplicate hook binding for pre_tool"):
        materialize_hook_plan(RuntimeHooksConfig(pre_tool=(MARKER_WRITER, MARKER_WRITER)))

    with pytest.raises(HookPlanValidationError, match="deterministic contiguous order"):
        ResolvedHookPlan(
            schema_version=HOOK_PLAN_SCHEMA_VERSION,
            plan_id="bad-order",
            revision=1,
            bindings=(HookPlanBinding(binding_id="session:pre_tool:2", event="pre_tool", command=("x",), order=2),),
            enabled=True,
            failure_mode="warn",
            timeout_seconds=30.0,
        )


def test_plan_drives_commands_and_timeout(tmp_path: Path) -> None:
    plan = materialize_hook_plan(RuntimeHooksConfig(pre_tool=(SLOW_HOOK,), timeout_seconds=0.25))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=None, plan=plan))

    assert outcome.action == "cancel"
    assert outcome.failed_error is not None
    assert "timed out after 0.25 seconds" in outcome.failed_error
    assert outcome.events[0].event_type == "runtime.tool_hook_pre"


def test_disabled_plan_with_disabled_hooks_is_a_no_op(tmp_path: Path) -> None:
    plan = materialize_hook_plan(RuntimeHooksConfig(pre_tool=(MARKER_WRITER,), enabled=False))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=None, plan=plan, sequence_start=9))

    assert plan.enabled is False
    assert (outcome.events, outcome.last_sequence) == ((), 9)


# --- configuration surface access --------------------------------------------


def test_commands_for_surface_maps_all_twenty_surfaces() -> None:
    hooks = RuntimeHooksConfig(
        pre_tool=(("a",),),
        post_tool=(("b",),),
        on_session_start=(("c",),),
        on_session_end=(("d",),),
        on_session_idle=(("e",),),
        on_background_task_registered=(("f",),),
        on_background_task_started=(("g",),),
        on_background_task_progress=(("h",),),
        on_background_task_completed=(("i",),),
        on_background_task_failed=(("j",),),
        on_background_task_cancelled=(("k",),),
        on_background_task_interrupted=(("l",),),
        on_background_task_notification_enqueued=(("m",),),
        on_background_task_result_read=(("n",),),
        on_delegated_result_available=(("o",),),
        on_turn_progress=(("p",),),
        on_tool_batch_repeated=(("q",),),
        on_approval_requested=(("r",),),
        on_question_asked=(("s",),),
        on_before_compact=(("t",),),
    )

    assert hooks.commands_for_surface("pre_tool") == (("a",),)
    assert hooks.commands_for_surface("post_tool") == (("b",),)
    assert hooks.commands_for_surface("session_start") == (("c",),)
    assert hooks.commands_for_surface("session_end") == (("d",),)
    assert hooks.commands_for_surface("session_idle") == (("e",),)
    assert hooks.commands_for_surface("background_task_registered") == (("f",),)
    assert hooks.commands_for_surface("background_task_started") == (("g",),)
    assert hooks.commands_for_surface("background_task_progress") == (("h",),)
    assert hooks.commands_for_surface("background_task_completed") == (("i",),)
    assert hooks.commands_for_surface("background_task_failed") == (("j",),)
    assert hooks.commands_for_surface("background_task_cancelled") == (("k",),)
    assert hooks.commands_for_surface("background_task_interrupted") == (("l",),)
    assert hooks.commands_for_surface("background_task_notification_enqueued") == (("m",),)
    assert hooks.commands_for_surface("background_task_result_read") == (("n",),)
    assert hooks.commands_for_surface("delegated_result_available") == (("o",),)
    assert hooks.commands_for_surface("turn_progress") == (("p",),)
    assert hooks.commands_for_surface("tool_batch_repeated") == (("q",),)
    assert hooks.commands_for_surface("approval_requested") == (("r",),)
    assert hooks.commands_for_surface("question_asked") == (("s",),)
    assert hooks.commands_for_surface("before_compact") == (("t",),)


def test_unknown_surface_keeps_key_error_semantics() -> None:
    with pytest.raises(KeyError):
        RuntimeHooksConfig().commands_for_surface("not_a_surface")  # type: ignore[arg-type]


def test_config_defaults_match_legacy() -> None:
    hooks = RuntimeHooksConfig()

    assert hooks.enabled is True
    assert hooks.timeout_seconds == 30.0
    assert hooks.failure_mode == "warn"
    assert hooks.pre_tool == ()
    assert hooks.post_tool == ()
    assert hooks.pre_tool_match == ()
    assert hooks.on_before_compact == ()
