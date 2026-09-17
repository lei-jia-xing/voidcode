"""CLI contract tests for ``voidcode sessions``.

Re-homes the session slice of the deleted ``test_cli_smoke.py``: exit codes,
stdout/stderr ownership, JSON payload shape and on-disk bundle effects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voidcode.cli import app
from voidcode.cli_support import EXIT_INVALID_RESOURCE, EXIT_RUNTIME_ERROR, EXIT_SUCCESS, EXIT_USAGE_ERROR
from voidcode.runtime.session import SessionRef, StoredSessionSummary

from ._cli_harness import StubRuntime, cli_boundary, deterministic_config, run_cli


class _SessionListRuntime(StubRuntime):
    """Stub runtime that serves a fixed session listing."""

    def __init__(self, summaries: tuple[StoredSessionSummary, ...]) -> None:
        super().__init__()
        self._summaries = summaries

    def list_sessions(self) -> Any:
        return iter(self._summaries)


class _ResumeErrorRuntime(StubRuntime):
    """Stub runtime whose ``resume_stream`` rejects the supplied approval request."""

    def resume_stream(self, *args: object, **kwargs: object) -> Any:
        raise ValueError(f"unknown approval request: {kwargs.get('approval_request_id')!r}")


@pytest.fixture(scope="module")
def seeded_session(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """A real deterministic run whose reported session id is reused by read-only tests."""
    workspace = tmp_path_factory.mktemp("sessions-seeded")
    (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
    result = run_cli("run", "read sample.txt", "--workspace", str(workspace), "--json", cwd=workspace)
    assert result.returncode == EXIT_SUCCESS, result.stderr
    session_id = json.loads(result.stdout)["session"]["session"]["id"]
    return workspace, session_id


# ---------------------------------------------------------------------------
# sessions list
# ---------------------------------------------------------------------------


def test_sessions_list_json_reports_main_scope_and_nested_rows(seeded_session: tuple[Path, str]) -> None:
    workspace, session_id = seeded_session

    result = run_cli("sessions", "list", "--workspace", str(workspace), "--json", cwd=workspace)

    payload = json.loads(result.stdout)
    assert result.returncode == EXIT_SUCCESS
    assert payload["workspace"] == str(workspace)
    assert payload["scope"] == "main"
    row = next(row for row in payload["sessions"] if row["session"]["id"] == session_id)
    assert row["prompt"] == "read sample.txt"
    assert row["status"] == "completed"
    # The id lives under the nested ``session`` ref; consumers must not read it off the row.
    assert "id" not in row


def test_sessions_list_scope_excludes_children_unless_requested(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    summaries = (
        StoredSessionSummary(
            session=SessionRef(id="leader-session"),
            status="completed",
            turn=1,
            prompt="read sample.txt",
            updated_at=1,
        ),
        StoredSessionSummary(
            session=SessionRef(id="child-session", parent_id="leader-session"),
            status="completed",
            turn=1,
            prompt="delegated read",
            updated_at=2,
        ),
    )
    runtime = _SessionListRuntime(summaries)

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        main_result = app.main(["sessions", "list", "--workspace", str(tmp_path), "--json"])
        main_payload = json.loads(capsys.readouterr().out)
        all_result = app.main(["sessions", "list", "--workspace", str(tmp_path), "--include-children", "--json"])
        all_payload = json.loads(capsys.readouterr().out)

    assert main_result == EXIT_SUCCESS
    assert main_payload["scope"] == "main"
    assert [row["session"]["id"] for row in main_payload["sessions"]] == ["leader-session"]
    assert all_result == EXIT_SUCCESS
    assert all_payload["scope"] == "all"
    assert {row["session"]["id"] for row in all_payload["sessions"]} == {"leader-session", "child-session"}
    child_row = next(row for row in all_payload["sessions"] if row["session"]["id"] == "child-session")
    assert child_row["session"]["parent_id"] == "leader-session"


# ---------------------------------------------------------------------------
# sessions debug
# ---------------------------------------------------------------------------


def test_sessions_debug_json_snapshot_contract(seeded_session: tuple[Path, str]) -> None:
    workspace, session_id = seeded_session

    result = run_cli("sessions", "debug", session_id, "--workspace", str(workspace), cwd=workspace)

    payload = json.loads(result.stdout)
    assert result.returncode == EXIT_SUCCESS
    assert payload["prompt"] == "read sample.txt"
    assert payload["persisted_status"] == "completed"
    assert payload["current_status"] == "completed"
    assert payload["active"] is False
    assert payload["terminal"] is True
    assert payload["replayable"] is True
    assert payload["resume_checkpoint_kind"] == "terminal"
    assert payload["pending_approval"] is None
    assert payload["pending_question"] is None
    assert payload["suggested_operator_action"] == "replay"
    assert payload["provider_context"]["segment_count"] >= 1
    assert "Traceback" not in result.stderr


def test_sessions_debug_unknown_session_is_clean_runtime_error(tmp_path: Path) -> None:
    result = run_cli("sessions", "debug", "missing-session", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == EXIT_RUNTIME_ERROR
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert "unknown session" in result.stderr
    assert "missing-session" in result.stderr
    assert "Traceback" not in result.stderr


# ---------------------------------------------------------------------------
# sessions resume
# ---------------------------------------------------------------------------


def test_sessions_resume_dry_run_reports_debug_without_executing(seeded_session: tuple[Path, str]) -> None:
    workspace, session_id = seeded_session

    result = run_cli("sessions", "resume", session_id, "--workspace", str(workspace), "--dry-run", cwd=workspace)

    payload = json.loads(result.stdout)
    assert result.returncode == EXIT_SUCCESS
    assert payload["dry_run"] is True
    assert payload["session_id"] == session_id
    assert payload["debug"]["prompt"] == "read sample.txt"
    assert "RESULT" not in result.stdout


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--approval-request-id", "req-1"), ("--approval-decision", "allow")],
)
def test_sessions_resume_rejects_partial_approval_flags(tmp_path: Path, flag: str, value: str) -> None:
    result = run_cli(
        "sessions",
        "resume",
        "demo-session",
        "--workspace",
        str(tmp_path),
        flag,
        value,
        cwd=tmp_path,
    )

    assert result.returncode == EXIT_USAGE_ERROR
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert "Traceback" not in result.stderr


def test_sessions_resume_unknown_approval_request_is_clean_runtime_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = _ResumeErrorRuntime()

    with cli_boundary(config=deterministic_config(), runtime=runtime):
        result = app.main(
            [
                "sessions",
                "resume",
                "demo-session",
                "--workspace",
                str(tmp_path),
                "--approval-request-id",
                "bogus",
                "--approval-decision",
                "allow",
            ]
        )

    captured = capsys.readouterr()
    assert result == EXIT_RUNTIME_ERROR
    assert captured.out == ""
    error_lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(error_lines) == 1
    assert error_lines[0].startswith("error:")
    assert "bogus" in captured.err
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# sessions export / import
# ---------------------------------------------------------------------------


def test_sessions_export_import_roundtrip_writes_and_reads_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "sample.txt").write_text("sample\n", encoding="utf-8")

    run_result = run_cli("run", "read sample.txt", "--workspace", str(source), "--json", cwd=source)
    assert run_result.returncode == EXIT_SUCCESS, run_result.stderr
    session_id = json.loads(run_result.stdout)["session"]["session"]["id"]

    bundle = tmp_path / "session.vcsession.zip"
    export_result = run_cli(
        "sessions",
        "export",
        session_id,
        "--workspace",
        str(source),
        "--output",
        str(bundle),
        "--support",
        cwd=source,
    )
    export_payload = json.loads(export_result.stdout)

    assert export_result.returncode == EXIT_SUCCESS
    assert bundle.exists()
    assert export_payload["session_id"] == session_id
    assert export_payload["output"] == str(bundle)
    assert export_payload["schema"] == "voidcode.session.bundle.v1"
    assert export_payload["manifest"]["support_mode"] is True

    dry_run_result = run_cli("sessions", "import", str(bundle), "--workspace", str(target), "--dry-run", cwd=target)
    dry_run_payload = json.loads(dry_run_result.stdout)

    assert dry_run_result.returncode == EXIT_SUCCESS
    assert dry_run_payload["import"]["dry_run"] is True
    assert dry_run_payload["import"]["imported_session_ids"] == [session_id]

    import_result = run_cli("sessions", "import", str(bundle), "--workspace", str(target), cwd=target)
    import_payload = json.loads(import_result.stdout)

    assert import_result.returncode == EXIT_SUCCESS
    assert import_payload["import"]["dry_run"] is False
    assert import_payload["import"]["imported_session_ids"] == [session_id]

    debug_result = run_cli("sessions", "debug", session_id, "--workspace", str(target), cwd=target)
    debug_payload = json.loads(debug_result.stdout)

    assert debug_result.returncode == EXIT_SUCCESS
    assert debug_payload["prompt"] == "read sample.txt"
    imported_bundle = debug_payload["session"]["metadata"]["imported_bundle"]
    assert imported_bundle["version"] == 1
    assert imported_bundle["original_session_id"] == session_id
    assert imported_bundle["original_workspace"] == str(source)


def test_sessions_export_json_format_prints_bundle_without_writing_file(seeded_session: tuple[Path, str]) -> None:
    workspace, session_id = seeded_session

    result = run_cli("sessions", "export", session_id, "--workspace", str(workspace), "--format", "json", cwd=workspace)

    payload = json.loads(result.stdout)
    assert result.returncode == EXIT_SUCCESS
    assert payload["schema"] == "voidcode.session.bundle.v1"
    assert payload["sessions"][0]["id"] == session_id
    assert list(workspace.glob("*.vcsession.zip")) == []


# ---------------------------------------------------------------------------
# sessions answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra_args", [(), ("--response-json", "not-json")])
def test_sessions_answer_requires_a_usable_response_payload(tmp_path: Path, extra_args: tuple[str, ...]) -> None:
    result = run_cli(
        "sessions",
        "answer",
        "question-session",
        "--workspace",
        str(tmp_path),
        "--question-request-id",
        "question-1",
        *extra_args,
        cwd=tmp_path,
    )

    assert result.returncode == EXIT_USAGE_ERROR
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert "Traceback" not in result.stderr


def test_sessions_answer_without_pending_question_exits_invalid_resource(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("sample\n", encoding="utf-8")
    run_result = run_cli("run", "read sample.txt", "--workspace", str(tmp_path), "--json", cwd=tmp_path)
    assert run_result.returncode == EXIT_SUCCESS, run_result.stderr
    session_id = json.loads(run_result.stdout)["session"]["session"]["id"]

    result = run_cli(
        "sessions",
        "answer",
        session_id,
        "--workspace",
        str(tmp_path),
        "--question-request-id",
        "question-1",
        "--response",
        "yes",
        cwd=tmp_path,
    )

    assert result.returncode == EXIT_INVALID_RESOURCE
    assert result.stdout == ""
    assert result.stderr.startswith("error:")
    assert "Traceback" not in result.stderr
