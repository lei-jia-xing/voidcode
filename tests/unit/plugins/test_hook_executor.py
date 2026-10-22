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

from voidcode.plugins.hooks.hook_config import RuntimeHooksConfig, hook_tool_matches
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
    policy: HookExecutionPolicy | None = None,
    environment: dict[str, str] | None = None,
) -> HookExecutionRequest:
    return HookExecutionRequest(
        hooks=hooks or RuntimeHooksConfig(),
        workspace=workspace,
        session_id="sess-1",
        tool_name=tool_name,
        phase=phase,  # type: ignore[arg-type]
        recursion_env_var=RECURSION_ENV_VAR,
        environment=environment or {},
        policy=policy or HookExecutionPolicy(),
    )


def _lifecycle_request(
    workspace: Path,
    *,
    hooks: RuntimeHooksConfig | None = None,
    surface: str = "session_start",
    payload: dict[str, object] | None = None,
    policy: HookExecutionPolicy | None = None,
) -> LifecycleHookExecutionRequest:
    return LifecycleHookExecutionRequest(
        hooks=hooks or RuntimeHooksConfig(),
        workspace=workspace,
        session_id="sess-1",
        surface=surface,  # type: ignore[arg-type]
        recursion_env_var=RECURSION_ENV_VAR,
        environment={},
        payload=payload or {},
        policy=policy or HookExecutionPolicy(),
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
    assert outcome.diagnostics == ("diag text",)
    (event,) = outcome.events
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
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks))

    assert outcome.action == "cancel"
    assert outcome.diagnostics == ("blocked by policy",)
    (event,) = outcome.events
    assert event.payload["action"] == "cancel"
    assert event.payload["diagnostic"] == "blocked by policy"
    assert event.payload["guidance"] == "fix the command"
    assert not (tmp_path / "marker.txt").exists()


def test_cancel_is_ignored_on_advisory_lifecycle_surface(tmp_path: Path) -> None:
    cancel = _emit({"action": "cancel", "diagnostic": "advisory", "guidance": "take it into account"})
    hooks = RuntimeHooksConfig(on_session_start=(cancel, MARKER_WRITER))
    outcome = run_lifecycle_hooks(_lifecycle_request(tmp_path, hooks=hooks))

    assert outcome.action == "continue"
    assert len(outcome.events) == 2
    assert "action" not in outcome.events[0].payload
    assert outcome.events[0].payload["diagnostic"] == "advisory"
    assert outcome.events[0].payload["guidance"] == "take it into account"
    assert outcome.diagnostics == ("advisory",)
    assert (tmp_path / "marker.txt").exists()


@pytest.mark.parametrize("surface", ["session_start", "session_end"])
def test_lifecycle_surfaces_keep_running_after_cancel(tmp_path: Path, surface: str) -> None:
    field = "on_session_start" if surface == "session_start" else "on_session_end"
    hooks = RuntimeHooksConfig(**{field: (_emit({"action": "cancel"}), MARKER_WRITER)})
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
    hooks = RuntimeHooksConfig(on_session_start=(hook,))
    outcome = run_lifecycle_hooks(_lifecycle_request(tmp_path, hooks=hooks, surface="session_start", payload={"phase": "start"}))

    assert outcome.events[0].payload["guidance"] == 'session_start|sess-1|{"phase": "start"}'
    assert outcome.events[0].payload["hook_status"] == "ok"
    assert outcome.events[0].payload["phase"] == "start"
    assert outcome.events[0].payload["surface"] == "session_start"


# --- gating: disabled, empty, recursion, globs, read-only --------------------


def test_empty_command_list_is_a_no_op(tmp_path: Path) -> None:
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig()))

    assert outcome.events == ()
    assert outcome.failed_error is None


def test_disabled_hooks_are_a_no_op(tmp_path: Path) -> None:
    marker = MARKER_WRITER
    disabled = run_tool_hooks(_tool_request(tmp_path, hooks=RuntimeHooksConfig(pre_tool=(marker,), enabled=False)))

    assert disabled.events == ()
    assert not (tmp_path / "marker.txt").exists()


def test_recursion_guard_skips_execution(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=(MARKER_WRITER,))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, environment={RECURSION_ENV_VAR: "1"}))

    assert outcome.events == ()
    assert not (tmp_path / "marker.txt").exists()


def test_tool_name_glob_filtering(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=(MARKER_WRITER,), pre_tool_match=("write*",))

    matching = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, tool_name="write"))
    assert len(matching.events) == 1
    assert (tmp_path / "marker.txt").exists()
    (tmp_path / "marker.txt").unlink()

    non_matching = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, tool_name="read"))
    assert non_matching.events == ()
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


def test_hook_tool_matches_empty_patterns_match_every_tool() -> None:
    assert hook_tool_matches((), "anything") is True
    assert hook_tool_matches(("write*",), "write_file") is True
    assert hook_tool_matches(("read", "grep"), "grep") is True
    assert hook_tool_matches(("read",), "write_file") is False


def test_read_only_policy_skips_commands(tmp_path: Path) -> None:
    hooks = RuntimeHooksConfig(pre_tool=(MARKER_WRITER, MARKER_WRITER))
    outcome = run_tool_hooks(_tool_request(tmp_path, hooks=hooks, policy=HookExecutionPolicy(read_only=True)))

    assert outcome.failed_error is None
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


# --- configuration defaults ---------------------------------------------------


def test_config_defaults() -> None:
    hooks = RuntimeHooksConfig()

    assert hooks.enabled is True
    assert hooks.timeout_seconds == 30.0
    assert hooks.pre_tool == ()
    assert hooks.post_tool == ()
    assert hooks.pre_tool_match == ()
    assert hooks.post_tool_match == ()
    assert hooks.on_session_start == ()
    assert hooks.on_session_end == ()
