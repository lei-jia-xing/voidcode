from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from voidcode.runtime.background.models import BackgroundTaskState
from voidcode.runtime.composition import CompositionOwner, CompositionRef, FrozenComposition, SessionCompositionOwner, TaskCompositionOwner
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse, UnknownSessionError
from voidcode.runtime.storage import SqliteSessionStore


def frozen_composition() -> FrozenComposition:
    return CompositionOwner().prepare((), intent={"fixture": "storage-consumer"})


def save_checkpoint(
    store: SqliteSessionStore,
    *,
    workspace: Path,
    session_id: str,
    prompt: str = "fixture",
    session_metadata: dict[str, object] | None = None,
    tool_results: tuple[dict[str, object], ...] = (),
    last_event_sequence: int = 0,
    **extras: object,
) -> None:
    metadata = dict(session_metadata or {})
    try:
        existing = store.load_session(workspace=workspace, session_id=session_id).session
    except UnknownSessionError:
        existing = None
    raw_ref = metadata.get("composition_ref")
    if raw_ref is None and existing is not None:
        raw_ref = existing.metadata.get("composition_ref")
    if raw_ref is None:
        snapshot = metadata.get("agent_capability_snapshot")
        if isinstance(snapshot, dict):
            raw_ref = snapshot.get("composition_ref")
    if raw_ref is not None:
        ref = CompositionRef.model_validate(raw_ref)
        composition = None
    else:
        composition = frozen_composition()
        ref = composition.reference(workspace=str(workspace), owner=SessionCompositionOwner(kind="session", session_id=session_id))
    metadata["composition_ref"] = ref.model_dump(mode="json")
    metadata.pop("execution_composition", None)
    store.save_interrupted_checkpoint(
        workspace=workspace,
        session_id=session_id,
        prompt=prompt,
        session_metadata=metadata,
        tool_results=tool_results,
        last_event_sequence=last_event_sequence,
        composition_ref=ref,
        composition=composition,
        **extras,
    )


def save_run(store: SqliteSessionStore, *, workspace: Path, request: RuntimeRequest, response: RuntimeResponse) -> None:
    session_id = response.session.session.id
    try:
        stored = store.load_session(workspace=workspace, session_id=session_id).session
    except UnknownSessionError:
        save_checkpoint(
            store,
            workspace=workspace,
            session_id=session_id,
            session_metadata=response.session.metadata,
            prompt=request.prompt,
            parent_session_id=response.session.session.parent_id,
        )
        stored = store.load_session(workspace=workspace, session_id=session_id).session
    session = replace(response.session, metadata={**stored.metadata, **response.session.metadata})
    store.save_run(workspace=workspace, request=request, response=replace(response, session=session))


def create_task(store: SqliteSessionStore, *, workspace: Path, task: BackgroundTaskState) -> BackgroundTaskState:
    raw_ref = task.request.metadata.get("composition_ref")
    if raw_ref is None:
        composition = frozen_composition()
        ref = composition.reference(workspace=str(workspace), owner=TaskCompositionOwner(kind="task", task_id=task.task.id))
    else:
        ref = CompositionRef.model_validate(raw_ref)
        composition = None
    store.create_background_task(workspace=workspace, task=replace(task, status="queued"), composition_ref=ref, composition=composition)
    if task.status in {"running", "idle"}:
        store.mark_background_task_running(workspace=workspace, task_id=task.task.id, session_id=task.session_id or f"child-{task.task.id}")
        if task.status == "idle":
            store.mark_background_task_idle(workspace=workspace, task_id=task.task.id)
    elif task.status != "queued":
        store.mark_background_task_terminal(workspace=workspace, task_id=task.task.id, status=task.status, error=task.error)
    return store.load_background_task(workspace=workspace, task_id=task.task.id)
