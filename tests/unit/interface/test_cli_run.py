"""Contract tests for ``voidcode run``.

Covers the deterministic end-to-end path, the ``--json`` payload, flag
forwarding into the runtime request, and the failure/blocked exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.cli_support import EXIT_APPROVAL_REQUIRED, EXIT_RUNTIME_ERROR

from ._cli_harness import (
    StubChunk,
    StubRuntime,
    chunk,
    cli_boundary,
    deterministic_config,
    event,
    run_cli,
    session_snapshot,
    stream,
)

SOURCE_FILE = "note.txt"
SOURCE_TEXT = "hello from the deterministic harness\n"


def seed_workspace(tmp_path: Path) -> Path:
    (tmp_path / SOURCE_FILE).write_text(SOURCE_TEXT, encoding="utf-8")
    return tmp_path


def request_metadata(runtime: StubRuntime) -> dict[str, object]:
    request = cast("Any", runtime.requests[0])
    return dict(request.metadata)


def test_deterministic_run_streams_the_tool_result_to_stdout(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)

    result = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), cwd=workspace)

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


@pytest.mark.parametrize(
    ("flags", "metadata"),
    [
        (("--mode", "plan"), {"mode": "plan"}),
        (("--read-only",), {"read_only": True}),
        (("--skills", "alpha"), {"skills": ["alpha"]}),
        (("--reasoning-effort", "high"), {"reasoning_effort": "high"}),
        (("--provider-stream",), {"provider_stream": True}),
        (("--no-provider-stream",), {"provider_stream": False}),
    ],
)
def test_run_flag_metadata_reaches_the_runtime(flags: tuple[str, ...], metadata: dict[str, Any], tmp_path: Path) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(stream(chunk(status="completed", output="ok\n")))

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        assert app.main(["run", f"read {SOURCE_FILE}", "--workspace", str(workspace), *flags]) == 0

    assert request_metadata(runtime) == metadata


def test_run_drops_absent_optional_metadata(tmp_path: Path) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(stream(chunk(status="completed", output="ok\n")))

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        assert app.main(["run", f"read {SOURCE_FILE}", "--workspace", str(workspace)]) == 0

    assert request_metadata(runtime) == {}


def test_show_thinking_is_presentation_only(tmp_path: Path) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(stream(chunk(status="completed", output="ok\n")))

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        assert app.main(["run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--show-thinking"]) == 0

    assert request_metadata(runtime) == {}


def test_trace_implies_provider_stream_metadata(tmp_path: Path) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(stream(chunk(status="completed", output="ok\n")))

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        assert app.main(["run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--trace"]) == 0

    assert request_metadata(runtime) == {"provider_stream": True}


def test_json_and_trace_cannot_be_combined(tmp_path: Path) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)

    assert app.main(["run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--json", "--trace"]) == 2


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


def test_failed_run_json_reports_the_diagnostics_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(
        stream(
            chunk(
                status="failed",
                events=(
                    event(
                        "runtime.failed",
                        error="Error: Runtime failed: from-error",
                        diagnostics={"summary": "from-summary"},
                    ),
                ),
            )
        ),
        debug_snapshot=session_snapshot(),
    )

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        code = app.main(["run", "read a.txt", "--workspace", str(workspace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_RUNTIME_ERROR
    assert payload["status"] == "failed"
    assert payload["error"] == "from-summary"


def test_plain_failed_run_writes_the_failure_footer_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(
        stream(chunk(status="failed", events=(event("runtime.failed", error="Error: boom", provider="openai", model="gpt-4o"),))),
        debug_snapshot=session_snapshot(resumable=True),
    )

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        code = app.main(["run", "read a.txt", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert code == EXIT_RUNTIME_ERROR
    assert captured.out == ""
    assert "VoidCode runtime failure summary" in captured.err
    assert "  session: s1" in captured.err
    assert "  resumable: true" in captured.err
    assert f"  resume: voidcode sessions resume s1 --workspace {workspace}" in captured.err


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


def test_json_approval_block_payload(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        code = app.main(["run", "write a.txt value", "--workspace", str(workspace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_APPROVAL_REQUIRED
    assert payload["blocked"] == {
        "kind": "approval_required",
        "session_id": "s1",
        "request_id": "req-1",
        "tool": "write",
        "target_summary": "a.txt",
    }


def test_json_question_block_payload_is_an_invalid_resource(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app

    workspace = seed_workspace(tmp_path)
    runtime = StubRuntime(
        stream(
            chunk(
                status="waiting",
                events=(
                    event(
                        "runtime.question_requested",
                        request_id="q-1",
                        tool="question",
                        question_count=2,
                        questions=[{"header": "pick", "question": "which?"}],
                    ),
                ),
            )
        )
    )

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        code = app.main(["run", "ask", "--workspace", str(workspace), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_RUNTIME_ERROR
    assert payload["blocked"]["kind"] == "question_required"
    assert payload["blocked"]["request_id"] == "q-1"


def test_json_redacts_reasoning_unless_show_thinking_is_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from voidcode.cli import app
    from voidcode.runtime.events import runtime_reasoning_part_payload

    workspace = seed_workspace(tmp_path)

    def reasoning_stream() -> list[StubChunk]:
        return [
            chunk(
                status="completed",
                events=(event("runtime.reasoning_part", **runtime_reasoning_part_payload(text="private chain")),),
            )
        ]

    for flags, expected_text in ((("--json",), None), (("--json", "--show-thinking"), "private chain")):
        runtime = StubRuntime(stream(*reasoning_stream()))
        with cli_boundary(config=deterministic_config(), runtime=runtime):
            assert app.main(["run", "think", "--workspace", str(workspace), *flags]) == 0
        payload = json.loads(capsys.readouterr().out)
        reasoning = next(item for item in payload["events"] if item["event_type"] == "runtime.reasoning_part")
        if expected_text is None:
            assert "private chain" not in json.dumps(payload)
            assert reasoning["payload"]["text_omitted"] is True
        else:
            assert reasoning["payload"]["text"] == expected_text


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


def test_unready_provider_human_form_prints_actions_to_stderr(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)

    result = run_cli(
        "run",
        f"read {SOURCE_FILE}",
        "--workspace",
        str(workspace),
        cwd=workspace,
        env={"VOIDCODE_EXECUTION_ENGINE": "provider"},
    )

    assert result.returncode == 11
    assert result.stdout == ""
    assert "actions:" in result.stderr
    assert f"voidcode doctor --workspace {workspace}" in result.stderr


def test_invalid_workspace_config_fails_with_a_doctor_action(tmp_path: Path) -> None:
    workspace = seed_workspace(tmp_path)
    (workspace / ".voidcode.json").write_text("{ not json", encoding="utf-8")

    result = run_cli("run", f"read {SOURCE_FILE}", "--workspace", str(workspace), "--json", cwd=workspace)

    assert result.returncode == 10
    payload = json.loads(result.stdout)
    assert payload["status"] == "not_ready"
    assert payload["first_task_readiness"]["details"]["workspace_config_valid"] is False
    assert [action["kind"] for action in payload["actions"]] == ["doctor"]
