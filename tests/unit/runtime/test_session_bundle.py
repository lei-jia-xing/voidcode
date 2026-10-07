from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from voidcode.runtime.background.models import BackgroundTaskRef, BackgroundTaskRequestSnapshot, BackgroundTaskState
from voidcode.runtime.bundle import (
    SessionBundleError,
    SessionBundleOptions,
    apply_session_bundle,
    build_session_bundle,
    parse_session_bundle,
    read_session_bundle_bytes,
    serialize_session_bundle,
)
from voidcode.runtime.composition import CompositionOwner, SessionCompositionOwner, TaskCompositionOwner
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.question import PendingQuestion
from voidcode.runtime.storage import SqliteSessionStore

EXACT = SessionBundleOptions(redact=False, include_tool_output=True, include_raw_provider_messages=True, include_reasoning_text=True)


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


def test_current_bundle_restores_real_fork_pending_task_and_dedupe_after_reopen(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    target.mkdir()
    store, fork_id = _source(source)
    built = build_session_bundle(sessions=store, tasks=store, workspace=source, session_id=fork_id, options=EXACT)
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


def test_default_export_refuses_lossy_scope_before_disclosing_secrets(tmp_path: Path) -> None:
    store, fork_id = _source(tmp_path / "source")
    with pytest.raises(SessionBundleError):
        build_session_bundle(sessions=store, tasks=store, workspace=tmp_path / "source", session_id=fork_id)


@pytest.mark.parametrize("schema", ["voidcode.session.bundle.v1", "voidcode.session.bundle.v999"])
def test_noncurrent_bundle_is_not_importable(schema: str) -> None:
    with pytest.raises(SessionBundleError):
        parse_session_bundle({"schema": schema})
