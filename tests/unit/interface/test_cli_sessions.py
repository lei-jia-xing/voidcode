"""CLI contract tests for ``voidcode sessions``.

Re-homes the session slice of the deleted ``test_cli_smoke.py``: exit codes,
stdout/stderr ownership, JSON payload shape and on-disk bundle effects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from voidcode.cli_support import EXIT_INVALID_RESOURCE, EXIT_RUNTIME_ERROR, EXIT_SUCCESS
from voidcode.runtime.session import StoredSessionSummary

from ._cli_harness import StubRuntime, run_cli, run_cli_process


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


# ---------------------------------------------------------------------------
# sessions answer
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# sessions rename
# ---------------------------------------------------------------------------


def _run_session_in(workspace: Path, prompt: str = "read sample.txt") -> str:
    """Run one real deterministic session in ``workspace`` and return its id."""
    (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
    result = run_cli("run", prompt, "--workspace", str(workspace), "--json", cwd=workspace)
    assert result.returncode == EXIT_SUCCESS, result.stderr
    return str(json.loads(result.stdout)["session"]["session"]["id"])


def test_sessions_rename_round_trips_into_listing_and_json(tmp_path: Path) -> None:
    """Rename writes through a real workspace, and ``list`` then prefers the title."""
    session_id = _run_session_in(tmp_path)

    rename_result = run_cli("sessions", "rename", session_id, "My CLI title", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    renamed = json.loads(rename_result.stdout)
    assert rename_result.returncode == EXIT_SUCCESS
    assert renamed["session"]["session"]["id"] == session_id
    assert renamed["session"]["title"] == "My CLI title"

    # Plain (non-JSON) listing is the human surface: the title replaces the prompt.
    list_result = run_cli("sessions", "list", "--workspace", str(tmp_path), cwd=tmp_path)
    assert list_result.returncode == EXIT_SUCCESS
    assert "title='My CLI title'" in list_result.stdout
    assert "prompt=" not in list_result.stdout

    json_list = json.loads(run_cli("sessions", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path).stdout)
    row = next(row for row in json_list["sessions"] if row["session"]["id"] == session_id)
    assert row["title"] == "My CLI title"
    # The prompt is still delivered: the client keeps it as the fallback source.
    assert row["prompt"] == "read sample.txt"


def test_sessions_rename_survives_a_fresh_cli_process(tmp_path: Path) -> None:
    """The title is durable state, not per-invocation: a new process lists it."""
    session_id = _run_session_in(tmp_path)
    _ = run_cli("sessions", "rename", session_id, "Persisted title", "--workspace", str(tmp_path), cwd=tmp_path)

    result = run_cli_process("sessions", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    assert result.returncode == EXIT_SUCCESS
    row = next(row for row in json.loads(result.stdout)["sessions"] if row["session"]["id"] == session_id)
    assert row["title"] == "Persisted title"


def test_sessions_list_falls_back_to_prompt_when_no_title(tmp_path: Path) -> None:
    session_id = _run_session_in(tmp_path)

    result = run_cli("sessions", "list", "--workspace", str(tmp_path), cwd=tmp_path)

    assert result.returncode == EXIT_SUCCESS
    assert f"SESSION id={session_id}" in result.stdout
    assert "prompt='read sample.txt'" in result.stdout


def test_sessions_rename_rejects_empty_title_and_unknown_session(tmp_path: Path) -> None:
    session_id = _run_session_in(tmp_path)

    empty = run_cli("sessions", "rename", session_id, "   ", "--workspace", str(tmp_path), cwd=tmp_path)
    assert empty.returncode == EXIT_RUNTIME_ERROR
    assert "non-empty" in empty.stderr

    missing = run_cli("sessions", "rename", "missing-session", "label", "--workspace", str(tmp_path), cwd=tmp_path)
    assert missing.returncode == EXIT_RUNTIME_ERROR
    assert "unknown session" in missing.stderr
    assert "Traceback" not in missing.stderr


# ---------------------------------------------------------------------------
# sessions tree
# ---------------------------------------------------------------------------


def test_sessions_tree_indents_a_parent_continued_after_its_forks(tmp_path: Path) -> None:
    """``sessions tree`` renders the forest, not the ``updated_at`` row order.

    After forking, the original session is continued so it becomes the newest
    row; the pre-fix renderer drew the forks above their parent with depth 1 for
    everything. The tree surfaces the parent's title at depth 0, both forks
    beneath it, and the provenance annotation.
    """
    root_id = _run_session_in(tmp_path)
    _ = run_cli("sessions", "rename", root_id, "Root session", "--workspace", str(tmp_path), cwd=tmp_path)
    first = json.loads(run_cli("sessions", "fork", root_id, "--workspace", str(tmp_path), "--json", cwd=tmp_path).stdout)["session"]
    second = json.loads(run_cli("sessions", "fork", root_id, "--workspace", str(tmp_path), "--json", cwd=tmp_path).stdout)["session"]
    # Continue the root, making it the most recently updated session in the workspace.
    _ = run_cli("run", "read sample.txt", "-r", root_id, "--workspace", str(tmp_path), cwd=tmp_path)
    root_row = next(
        row
        for row in json.loads(run_cli("sessions", "list", "--workspace", str(tmp_path), "--json", cwd=tmp_path).stdout)["sessions"]
        if row["session"]["id"] == root_id
    )
    assert root_row["updated_at"] > max(first["updated_at"], second["updated_at"]), "root must be the newest row for this to be the bug"

    plain = run_cli("sessions", "tree", "--workspace", str(tmp_path), cwd=tmp_path)
    assert plain.returncode == EXIT_SUCCESS
    lines = plain.stdout.splitlines()
    assert lines[0] == f"{root_id} 'Root session'"
    # Same fork boundary, so siblings order by session id.
    expected_forks = sorted((first, second), key=lambda fork: fork["session"]["id"])
    assert lines[1] == f"  {expected_forks[0]['session']['id']} 'Root session' <- {root_id}@{expected_forks[0]['forked_at_sequence']}"
    assert lines[2] == f"  {expected_forks[1]['session']['id']} 'Root session' <- {root_id}@{expected_forks[1]['forked_at_sequence']}"

    payload = json.loads(run_cli("sessions", "tree", "--workspace", str(tmp_path), "--json", cwd=tmp_path).stdout)
    assert payload["root"] is None
    assert [row["depth"] for row in payload["lineage"]] == [0, 1, 1]
    by_id = {row["session_id"]: row for row in payload["lineage"]}
    assert by_id[root_id]["title"] == "Root session"
    # Keys are additive: the original four survive on every row.
    assert by_id[root_id]["forked_from_session_id"] is None
    assert by_id[first["session"]["id"]]["forked_from_session_id"] == root_id


def test_sessions_tree_named_session_is_one_chain_oldest_ancestor_first(tmp_path: Path) -> None:
    """With a ``session_id`` the output is that session's ancestry, indented 0..N."""
    root_id = _run_session_in(tmp_path)
    _ = run_cli("sessions", "rename", root_id, "Ancestor", "--workspace", str(tmp_path), cwd=tmp_path)
    child = json.loads(run_cli("sessions", "fork", root_id, "--workspace", str(tmp_path), "--json", cwd=tmp_path).stdout)["session"]
    grandchild = json.loads(run_cli("sessions", "fork", child["session"]["id"], "--workspace", str(tmp_path), "--json", cwd=tmp_path).stdout)[
        "session"
    ]

    result = run_cli("sessions", "tree", grandchild["session"]["id"], "--workspace", str(tmp_path), "--json", cwd=tmp_path)

    payload = json.loads(result.stdout)
    assert result.returncode == EXIT_SUCCESS
    assert payload["root"] == grandchild["session"]["id"]
    assert [(row["session_id"], row["depth"]) for row in payload["lineage"]] == [
        (root_id, 0),
        (child["session"]["id"], 1),
        (grandchild["session"]["id"], 2),
    ]
    assert payload["lineage"][0]["title"] == "Ancestor"
