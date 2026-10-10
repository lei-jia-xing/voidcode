from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from voidcode.cli.app import root_cli
from voidcode.runtime.background.models import BackgroundTaskRef, BackgroundTaskRequestSnapshot, BackgroundTaskState
from voidcode.runtime.bundle import (
    SessionBundleError,
    SessionBundleOptions,
    apply_session_bundle,
    build_session_bundle,
    parse_session_bundle,
    read_session_bundle_bytes,
    serialize_session_bundle,
    write_session_bundle,
)
from voidcode.runtime.composition import CompositionOwner, SessionCompositionOwner, TaskCompositionOwner
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.question import PendingQuestion
from voidcode.runtime.storage import SqliteSessionStore


def _bundle_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(tmp_path / "source-state" / "sessions.sqlite3"))
    store, fork_id = _source(source)
    bundle = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id)
    bundle_path = tmp_path / "source.vcsession.zip"
    write_session_bundle(bundle, path=bundle_path)
    return bundle_path


def _tree_state(path: Path) -> tuple[tuple[object, ...], ...] | None:
    if not path.exists():
        return None
    entries = sorted((path, *path.rglob("*")), key=str)
    state: list[tuple[object, ...]] = []
    for entry in entries:
        stat = entry.lstat()
        digest = hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None
        relative = "." if entry == path else entry.relative_to(path).as_posix()
        state.append((relative, stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, digest))
    return tuple(state)


def _cli_import_dry_run(bundle_path: Path, workspace: Path) -> dict[str, object]:
    result = CliRunner().invoke(
        root_cli,
        ["sessions", "import", str(bundle_path), "--dry-run", "--workspace", str(workspace)],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["import"]


EXACT = SessionBundleOptions()


def _source(workspace: Path) -> tuple[SqliteSessionStore, str]:
    workspace.mkdir()
    store = SqliteSessionStore()
    frozen = CompositionOwner().prepare((), intent={"fixture": "bundle-consumer"})
    root_ref = frozen.reference(workspace=str(workspace), owner=SessionCompositionOwner(kind="session", session_id="root"))
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id="root",
        prompt="root prompt",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        composition_ref=root_ref,
        composition=frozen,
    )
    store.append_session_event(
        workspace=workspace,
        session_id="root",
        event_type="runtime.request_received",
        source="runtime",
        payload={"prompt": "root prompt"},
        dedupe_key="root-request",
    )
    task_ref = frozen.reference(workspace=str(workspace), owner=TaskCompositionOwner(kind="task", task_id="work"))
    task = BackgroundTaskState(task=BackgroundTaskRef("work"), request=BackgroundTaskRequestSnapshot(prompt="child prompt", parent_session_id="root"))
    store.create_background_task(workspace=workspace, task=task, composition_ref=task_ref, composition=frozen)
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id="child",
        parent_session_id="root",
        prompt="child prompt",
        session_metadata={},
        tool_results=(),
        last_event_sequence=0,
        composition_ref=task_ref,
    )
    store.append_session_event(
        workspace=workspace,
        session_id="child",
        event_type="runtime.request_received",
        source="runtime",
        payload={"prompt": "child prompt"},
        dedupe_key="child-request",
    )
    fork = store.fork_session(workspace=workspace, session_id="child", at_sequence=1)
    response = store.load_session(workspace=workspace, session_id="child")
    request = RuntimeRequest(prompt="child prompt", session_id="child", parent_session_id="root")
    pending = PendingQuestion(request_id="question-1", tool_name="AskUserQuestion", arguments={"questions": []})
    store.save_pending_question(
        workspace=workspace,
        request=request,
        response=RuntimeResponse(session=replace(response.session, status="waiting"), events=response.events, output=None),
        pending_question=pending,
    )
    return store, fork.session.id


def _apply(bundle, store: SqliteSessionStore, workspace: Path, **kwargs):
    return apply_session_bundle(
        bundle,
        session_repository=store,
        events=store,
        recovery=store,
        run_writer=store,
        workspace=workspace,
        admit_composition=CompositionOwner().refresh,
        **kwargs,
    )


def test_cli_import_dry_run_leaves_absent_state_uninitialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_path = _bundle_file(tmp_path, monkeypatch)
    workspace = tmp_path / "empty-target"
    workspace.mkdir()
    state_root = tmp_path / "empty-state"
    database_path = state_root / "sessions.sqlite3"
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(database_path))
    workspace_before = _tree_state(workspace)

    result = _cli_import_dry_run(bundle_path, workspace)

    assert result["dry_run"] is True
    assert result["schema_version"] == 2
    assert _tree_state(state_root) is None
    assert _tree_state(workspace) == workspace_before


def test_cli_import_dry_run_reads_committed_wal_without_mutating_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_path = _bundle_file(tmp_path, monkeypatch)
    workspace = tmp_path / "wal-target"
    workspace.mkdir()
    state_root = tmp_path / "wal-state"
    database_path = state_root / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    assert store.list_sessions(workspace=workspace) == ()

    writer = sqlite3.connect(database_path)
    try:
        assert writer.execute("PRAGMA user_version").fetchone()[0] == 2
        assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        database_before_insert = hashlib.sha256(database_path.read_bytes()).hexdigest()
        writer.execute(
            """
            INSERT INTO sessions (
                session_id, workspace_id, status, turn, prompt, metadata_json,
                created_at, updated_at, last_event_sequence
            ) VALUES ('root', ?, 'completed', 0, '', '{}', 1, 1, 0)
            """,
            (str(workspace),),
        )
        writer.commit()
        assert hashlib.sha256(database_path.read_bytes()).hexdigest() == database_before_insert
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE sessions SET prompt = 'uncommitted' WHERE session_id = 'root'")
        monkeypatch.setenv("VOIDCODE_DB_PATH", str(database_path))
        state_before = _tree_state(state_root)
        workspace_before = _tree_state(workspace)

        result = _cli_import_dry_run(bundle_path, workspace)

        imported_ids = result["imported_session_ids"]
        assert isinstance(imported_ids, list)
        assert "root-imported" in imported_ids
        assert "root" not in imported_ids
        assert writer.in_transaction
        assert writer.execute("SELECT prompt FROM sessions WHERE session_id = 'root'").fetchone()[0] == "uncommitted"
        assert _tree_state(state_root) == state_before
        assert _tree_state(workspace) == workspace_before
    finally:
        writer.rollback()
        writer.close()


def test_export_session_bundle_rows_rejects_source_changes_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database_path = tmp_path / "state" / "sessions.sqlite3"
    store = SqliteSessionStore(database_path=database_path)
    assert store.list_sessions(workspace=workspace) == ()
    copyfile = shutil.copyfile

    def copy_then_change_source(source: str | Path, destination: str | Path) -> str | Path:
        copied_path = copyfile(source, destination)
        if Path(source) == database_path:
            with database_path.open("ab") as source_db:
                source_db.write(b"changed")
        return copied_path

    monkeypatch.setattr(shutil, "copyfile", copy_then_change_source)
    with pytest.raises(RuntimeError):
        store.export_session_bundle_rows(workspace=workspace, session_ids=("root",), task_ids=())


def test_current_bundle_restores_real_fork_pending_task_and_dedupe_after_reopen(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    target.mkdir()
    store, fork_id = _source(source)
    built = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id)
    parsed = read_session_bundle_bytes(serialize_session_bundle(built))
    _apply(parsed, SqliteSessionStore(), target)
    reopened = SqliteSessionStore()
    child = reopened.load_session(workspace=target, session_id="child")
    pending = reopened.load_pending_question(workspace=target, session_id="child")
    assert pending is not None and pending.request_id == "question-1"
    assert child.session.status == "waiting"
    checkpoint = reopened.load_resume_checkpoint(workspace=target, session_id="child")
    assert checkpoint is not None and checkpoint["pending_question_request_id"] == "question-1"
    ref = child.session.metadata["composition_ref"]
    from voidcode.runtime.composition import CompositionRef

    frozen = reopened.load_execution_composition(ref=CompositionRef.model_validate(ref))
    assert frozen == store.load_execution_composition(
        ref=CompositionRef.model_validate(store.load_session(workspace=source, session_id="child").session.metadata["composition_ref"])
    )
    restored_task = reopened.load_background_task(workspace=target, task_id="work")
    assert restored_task.status == "queued"
    assert restored_task.request.metadata["bundle_import_provenance"]["source_task_id"] == "work"
    assert not reopened.list_queued_background_tasks(workspace=target)
    assert reopened.load_pending_question(workspace=target, session_id=fork_id) is None
    rows = reopened.export_session_bundle_rows(workspace=target, session_ids=("root", "child", fork_id), task_ids=("work",))
    child_events = [row for row in rows["events"] if row["session_id"] == "child"]
    assert [(row["sequence"], row["parent_sequence"]) for row in child_events] == [(1, None)]
    fork_row = next(row for row in rows["sessions"] if row["session_id"] == fork_id)
    assert fork_row["forked_from_session_id"] == "child" and fork_row["forked_at_sequence"] == 1
    assert (
        reopened.append_session_event(
            workspace=target,
            session_id="root",
            event_type="runtime.request_received",
            source="runtime",
            payload={"prompt": "duplicate"},
            dedupe_key="root-request",
        )
        is None
    )


def test_current_bundle_resolves_both_owner_collisions_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    store, fork_id = _source(source)
    _source(target)
    bundle = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id, options=EXACT)
    original = serialize_session_bundle(bundle, fmt="json")
    result = _apply(bundle, SqliteSessionStore(), target)
    assert "child-imported" in result.imported_session_ids
    imported = SqliteSessionStore().load_session(workspace=target, session_id="child-imported")
    assert imported.session.session.parent_id == "root-imported"
    assert imported.session.metadata["composition_ref"]["owner"] == {"kind": "task", "task_id": "work-imported"}
    assert serialize_session_bundle(bundle, fmt="json") == original


def test_late_invalid_row_and_resolver_collision_leave_destination_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    store, fork_id = _source(source)
    _source(target)
    bundle = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id, options=EXACT)
    destination = SqliteSessionStore()
    before = destination.export_session_bundle_rows(workspace=target, session_ids=("root", "child"), task_ids=("work",))
    with pytest.raises(SessionBundleError):
        _apply(bundle, destination, target, task_id_resolver=lambda _: "work")
    bundle.sessions[-1].events[-1]["parent_sequence"] = 99
    with pytest.raises(SessionBundleError):
        _apply(bundle, destination, target)
    assert destination.export_session_bundle_rows(workspace=target, session_ids=("root", "child"), task_ids=("work",)) == before
    assert not destination.has_session(workspace=target, session_id="root-imported")


@pytest.mark.parametrize("schema", ["voidcode.session.bundle.v1", "voidcode.session.bundle.v999"])
def test_noncurrent_bundle_is_not_importable(schema: str) -> None:
    with pytest.raises(SessionBundleError):
        parse_session_bundle({"schema": schema})


def test_session_bundle_roundtrips_artifacts_with_byte_equality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    target.mkdir()
    store, fork_id = _source(source)

    from voidcode.tools.output import create_tool_output_artifact

    raw_bytes = b"important binary payload \x00\x01\x02\x03\xff\xfe"
    artifact = create_tool_output_artifact(
        session_id="child",
        tool_call_id="call-art-1",
        tool_name="sample_tool",
        content=raw_bytes,
        mime="application/octet-stream",
    )
    from voidcode.core.turns import ReportedCall
    from voidcode.runtime.execution.report_codec import report_payload
    from voidcode.tools.contracts import OutputBounds, OutputReference, TextOutput, ToolSuccess

    ref = OutputReference(uri=f"voidcode://artifact/{artifact['artifact_id']}", artifact=artifact)
    output = TextOutput("artifact generated", bounds=OutputBounds(reference=ref))
    report = ReportedCall("call-art-1", "sample_tool", {}, ToolSuccess("sample_tool", output=output))
    store.append_session_event(
        workspace=source,
        session_id="child",
        event_type="runtime.tool_completed",
        source="tool",
        payload={
            "tool": "sample_tool",
            "status": "ok",
            "artifact_id": artifact["artifact_id"],
            "artifact": artifact,
            "reported_call": report_payload(report),
        },
        dedupe_key="child-tool-1",
    )
    built = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id, options=EXACT)
    assert built.manifest.artifact_count >= 1
    matched = next((a for a in built.artifacts if a.artifact_id == artifact["artifact_id"]), None)
    assert matched is not None
    assert matched.status == "available"
    assert matched.data_base64 is not None
    assert matched.sha256 == artifact["sha256"]
    serialized = serialize_session_bundle(built)
    parsed = read_session_bundle_bytes(serialized)
    # Switch to clean target environment to simulate cold import on another machine
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "target_state"))
    _apply(parsed, SqliteSessionStore(), target)
    from voidcode.runtime.paths import session_artifacts_dir

    target_dir = session_artifacts_dir("child")
    assert target_dir.is_dir()
    files = list(target_dir.glob(f"*{artifact['artifact_id']}*"))
    assert len(files) == 1
    assert files[0].read_bytes() == raw_bytes

    # Cold read via artifact metadata finds restored artifact in new environment
    from voidcode.tools.output import read_tool_output_artifact

    read_res = read_tool_output_artifact(dict(artifact))
    from voidcode.core.tool_context import ArtifactBinary

    assert isinstance(read_res, ArtifactBinary)
    assert read_res.data == raw_bytes


def test_session_bundle_rejects_corrupted_artifact_checksum_without_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    target.mkdir()
    store, fork_id = _source(source)

    from voidcode.tools.output import create_tool_output_artifact

    artifact = create_tool_output_artifact(
        session_id="child",
        tool_call_id="call-art-2",
        tool_name="sample_tool",
        content=b"pristine content",
        mime="text/plain",
    )
    from voidcode.core.turns import ReportedCall
    from voidcode.runtime.execution.report_codec import report_payload
    from voidcode.tools.contracts import OutputBounds, OutputReference, TextOutput, ToolSuccess

    ref = OutputReference(uri=f"voidcode://artifact/{artifact['artifact_id']}", artifact=artifact)
    output = TextOutput("artifact generated", bounds=OutputBounds(reference=ref))
    report = ReportedCall("call-art-2", "sample_tool", {}, ToolSuccess("sample_tool", output=output))
    store.append_session_event(
        workspace=source,
        session_id="child",
        event_type="runtime.tool_completed",
        source="tool",
        payload={
            "tool": "sample_tool",
            "status": "ok",
            "artifact_id": artifact["artifact_id"],
            "artifact": artifact,
            "reported_call": report_payload(report),
        },
        dedupe_key="child-tool-2",
    )
    built = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id, options=EXACT)
    payload = built.to_payload()
    # Corrupt the sha256 checksum in the bundle payload: parse rejects immediately
    payload["artifacts"][0]["sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"
    with pytest.raises(SessionBundleError, match=r"checksum mismatch"):
        parse_session_bundle(payload)

    # In-memory tamper before apply: apply rejects before writing files or DB
    dest_store = SqliteSessionStore()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "target_state_corrupt"))
    valid_bundle = parse_session_bundle(built.to_payload())
    tampered_art = replace(valid_bundle.artifacts[0], data_base64=base64.b64encode(b"corrupted").decode())
    tampered_bundle = replace(valid_bundle, artifacts=(tampered_art,))
    with pytest.raises(SessionBundleError, match=r"(checksum mismatch|byte count mismatch)"):
        _apply(tampered_bundle, dest_store, target)
    # Verify no side effects: session was not created in destination store
    assert not dest_store.has_session(workspace=target, session_id="child")
    from voidcode.runtime.paths import session_artifacts_dir

    target_dir = session_artifacts_dir("child")
    assert not target_dir.exists() or list(target_dir.iterdir()) == []


def test_session_bundle_rejects_redacted_omitted_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    store, fork_id = _source(source)
    from voidcode.core.turns import ReportedCall
    from voidcode.runtime.execution.report_codec import report_payload
    from voidcode.tools.contracts import OutputBounds, OutputReference, TextOutput, ToolSuccess
    from voidcode.tools.output import create_tool_output_artifact

    artifact = create_tool_output_artifact(
        session_id="child",
        tool_call_id="call-secret",
        tool_name="secret_tool",
        content=b"secret payload",
        mime="text/plain",
    )
    ref = OutputReference(uri=f"voidcode://artifact/{artifact['artifact_id']}", artifact=artifact)
    report = ReportedCall(
        "call-secret",
        "secret_tool",
        {},
        ToolSuccess("secret_tool", output=TextOutput("artifact generated", bounds=OutputBounds(reference=ref))),
    )
    store.append_session_event(
        workspace=source,
        session_id="child",
        event_type="runtime.tool_completed",
        source="tool",
        payload={
            "tool": "secret_tool",
            "status": "ok",
            "artifact_id": artifact["artifact_id"],
            "artifact": artifact,
            "reported_call": report_payload(report),
        },
        dedupe_key="child-tool-secret",
    )
    payload = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id).to_payload()
    artifact_payload = next(item for item in payload["artifacts"] if item["artifact_id"] == artifact["artifact_id"])
    artifact_payload["data_base64"] = None
    artifact_payload["status"] = "omitted"
    with pytest.raises(SessionBundleError, match="invalid artifact status"):
        parse_session_bundle(payload)


@pytest.mark.parametrize(
    "options",
    (
        SessionBundleOptions(redact=True),
        SessionBundleOptions(include_tool_output=False),
        SessionBundleOptions(include_raw_provider_messages=False),
        SessionBundleOptions(include_reasoning_text=False),
    ),
)
def test_session_bundle_export_rejects_inexact_options(tmp_path: Path, options: SessionBundleOptions) -> None:
    workspace = tmp_path / "workspace"
    store, session_id = _source(workspace)
    with pytest.raises(SessionBundleError, match="exact unredacted"):
        build_session_bundle(sessions=store, tasks=store, workspace=workspace, session_id=session_id, options=options)
