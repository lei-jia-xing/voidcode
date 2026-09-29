"""Contract tests for ``voidcode run``.

Covers the deterministic end-to-end path, the ``--json`` payload, flag
forwarding into the runtime request, and the failure/blocked exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.cli_support import EXIT_APPROVAL_REQUIRED, EXIT_INVALID_RESOURCE, EXIT_RUNTIME_ERROR, EXIT_SUCCESS, EXIT_USAGE_ERROR

from ._cli_harness import (
    CliRun,
    StubRuntime,
    chunk,
    cli_boundary,
    deterministic_config,
    event,
    run_cli,
    run_cli_process,
    session_snapshot,
    stream,
)

SOURCE_FILE = "note.txt"
SOURCE_TEXT = "hello from the deterministic harness\n"


def seed_workspace(tmp_path: Path) -> Path:
    (tmp_path / SOURCE_FILE).write_text(SOURCE_TEXT, encoding="utf-8")
    return tmp_path


def session_id_of(result: CliRun) -> str:
    """The session id the ``--json`` payload reports for one run."""
    return cast("str", json.loads(result.stdout)["session"]["session"]["id"])


def event_watermark(workspace: Path, session_id: str) -> int:
    """The persisted event-log watermark of one session, read back through the CLI."""
    debug = run_cli("sessions", "debug", session_id, "--workspace", str(workspace), cwd=workspace)
    assert debug.returncode == EXIT_SUCCESS, debug.stderr
    return cast("int", json.loads(debug.stdout)["last_event_sequence"])


def request_metadata(runtime: StubRuntime) -> dict[str, object]:
    request = cast("Any", runtime.requests[0])
    return dict(request.metadata)


def test_deterministic_run_streams_the_tool_result_to_stdout(tmp_path: Path) -> None:
    """The real ``python -m voidcode run`` process completes the deterministic path.

    Kept as a spawn: it proves the whole entrypoint still works end-to-end —
    environment-driven engine selection, the deterministic graph, and exit/stream
    wiring — with nothing shared from this interpreter.
    """
    workspace = seed_workspace(tmp_path)

    result = run_cli_process("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), cwd=workspace)

    assert result.returncode == 0
    assert result.stdout == f"Read 1 line(s) from {SOURCE_FILE}.\n"
    assert result.stderr == ""


def test_run_json_payload_is_the_machine_contract(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)

    result = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--json", cwd=workspace)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["workspace"] == str(workspace)
    assert payload["output"] == f"Read 1 line(s) from {SOURCE_FILE}."
    assert payload["session"]["status"] == "completed"
    assert payload["session"]["session"]["id"]
    assert {"event_type", "source", "payload"} <= set(payload["events"][0])
    assert "runtime.request_received" in {item["event_type"] for item in payload["events"]}


def test_run_forwards_the_session_id_argument(tmp_path: Path) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(stream(chunk(status="completed", output="ok\n")))

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        assert app.main(["run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--session-id", "named-session"]) == 0

    assert cast("Any", runtime.requests[0]).session_id == "named-session"


def test_failed_run_reports_status_and_error(tmp_path: Path) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(
        stream(chunk(status="failed", events=(event("runtime.failed", error="Error: Runtime failed: boom"),))),
        debug_snapshot=session_snapshot(),
    )

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        code = app.main(["run", "read a.txt", "--workspace", str(workspace), "--json"])

    assert code == EXIT_RUNTIME_ERROR


def test_non_interactive_approval_blocks_with_resume_guidance(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(
        stream(
            chunk(
                status="waiting",
                events=(event("runtime.approval_requested", request_id="req-1", tool="write", target_summary="a.txt"),),
            )
        )
    )

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        code = app.main(["run", "write a.txt value", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert code == EXIT_APPROVAL_REQUIRED
    assert captured.out == ""
    assert f"resume with: voidcode sessions resume s1 --workspace {workspace} --approval-request-id req-1" in captured.err


def test_keyboard_interrupt_cancels_the_session_and_exits_130(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)

    class InterruptingRuntime(StubRuntime):
        def run_stream(self, request: object) -> Any:
            del request

            def interrupted() -> Any:
                yield chunk(status="running", events=(event("runtime.tool_started", tool="read"),))
                raise KeyboardInterrupt

            return interrupted()

    runtime = InterruptingRuntime()
    with cli_boundary(config=deterministic_config(), runtime=runtime):
        code = app.main(["run", "read a.txt", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert code == 130
    assert "Interrupted current run." in captured.err
    assert runtime.cancellations == [{"session_id": "s1", "run_id": None, "reason": "cli KeyboardInterrupt"}]


def test_unready_provider_stops_before_the_model(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)

    result = run_cli(
        "run",
        f"read {SOURCE_FILE}",
        "--workspace",
        str(workspace),
        "--json",
        cwd=workspace,
        env={"VOIDCODE_EXECUTION_ENGINE": "provider"},
    )

    assert result.returncode == 11
    payload = json.loads(result.stdout)
    assert payload["first_task_readiness"]["status"] == "not_ready"
    assert payload["error"]
    assert [action["kind"] for action in payload["actions"]] == ["config_init", "doctor"]
    assert all(action["command"].startswith("voidcode ") for action in payload["actions"])
    assert "sk-" not in result.stdout


# ---------------------------------------------------------------------------
# -c / --continue and -r / --resume
# ---------------------------------------------------------------------------


def test_continue_runs_the_next_prompt_in_the_most_recent_session(tmp_path: Path) -> None:
    """Two ``run`` invocations with ``-c`` land in one session and grow its event log."""
    workspace = seed_workspace(tmp_path)
    first = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--json", cwd=workspace)
    assert first.returncode == EXIT_SUCCESS, first.stderr
    session_id = session_id_of(first)
    watermark = event_watermark(workspace, session_id)

    continued = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "-c", "--json", cwd=workspace)

    assert continued.returncode == EXIT_SUCCESS, continued.stderr
    payload = json.loads(continued.stdout)
    assert payload["session"]["session"]["id"] == session_id
    assert payload["session"]["status"] == "completed"
    assert payload["output"] == f"Read 1 line(s) from {SOURCE_FILE}."
    assert event_watermark(workspace, session_id) > watermark


def test_resume_targets_the_named_session_while_a_newer_one_exists(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)
    older = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--session-id", "older", "--json", cwd=workspace)
    newer = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--session-id", "newer", "--json", cwd=workspace)
    assert older.returncode == EXIT_SUCCESS, older.stderr
    assert newer.returncode == EXIT_SUCCESS, newer.stderr
    listing = run_cli("sessions", "list", "--workspace", str(workspace), "--json", cwd=workspace)
    assert [row["session"]["id"] for row in json.loads(listing.stdout)["sessions"]][0] == "newer"
    newer_watermark = event_watermark(workspace, "newer")

    resumed = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "-r", "older", "--json", cwd=workspace)

    assert resumed.returncode == EXIT_SUCCESS, resumed.stderr
    assert session_id_of(resumed) == "older"
    assert event_watermark(workspace, "newer") == newer_watermark


def test_resume_rejects_an_unknown_session_id(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)

    result = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "-r", "no-such-session", cwd=workspace)

    assert result.returncode == EXIT_RUNTIME_ERROR
    assert result.stdout == ""
    assert "unknown session: no-such-session" in result.stderr


def test_continue_without_any_session_fails_with_guidance(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)

    result = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--continue", cwd=workspace)

    assert result.returncode == EXIT_INVALID_RESOURCE
    assert result.stdout == ""
    assert result.stderr.startswith("error: no session to continue in this workspace")
    assert f"start one with: voidcode run <request> --workspace {workspace}" in result.stderr


@pytest.mark.parametrize(
    "flags",
    [
        ("-c", "-r", "older"),
        ("-c", "--session-id", "older"),
        ("--resume", "older", "--session-id", "older"),
    ],
)
def test_continuation_flags_are_mutually_exclusive(tmp_path: Path, flags: tuple[str, ...]) -> None:
    workspace = seed_workspace(tmp_path)

    result = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), *flags, cwd=workspace)

    assert result.returncode == EXIT_USAGE_ERROR
    assert result.stdout == ""
    assert result.stderr == "error: --continue, --resume, and --session-id each select a session; pass only one\n"


def test_continue_refuses_a_session_waiting_for_approval(tmp_path: Path) -> None:
    """A not-continuable target names ``sessions resume`` instead of being re-entered."""
    workspace = seed_workspace(tmp_path)
    waiting = run_cli(
        "run",
        f"write {SOURCE_FILE} :: value",
        "--workspace",
        str(workspace),
        "--approval-mode",
        "ask",
        "--session-id",
        "blocked",
        "--json",
        cwd=workspace,
    )
    assert waiting.returncode == EXIT_APPROVAL_REQUIRED, waiting.stderr
    blocked = json.loads(waiting.stdout)["blocked"]
    assert blocked["kind"] == "approval_required"
    expected_guidance = (
        f"resume with: voidcode sessions resume blocked --workspace {workspace} "
        f"--approval-request-id {blocked['request_id']} --approval-decision allow"
    )

    for flags in (("-c",), ("-r", "blocked")):
        result = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), *flags, cwd=workspace)
        assert result.returncode == EXIT_INVALID_RESOURCE
        assert result.stdout == ""
        assert result.stderr == f"error: session blocked is waiting for approval of write {SOURCE_FILE}; {expected_guidance}\n"

    untouched = run_cli("sessions", "debug", "blocked", "--workspace", str(workspace), cwd=workspace)
    assert json.loads(untouched.stdout)["persisted_status"] == "waiting"
