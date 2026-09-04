from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.plan import (
    HookPlanValidationError,
    hook_plan_from_session_metadata,
    materialize_hook_plan,
)
from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.runtime.background.supervisor import RuntimeBackgroundTaskSupervisor
from voidcode.runtime.contracts import RuntimeRequest, RuntimeResponse
from voidcode.runtime.events import (
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED,
)
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.storage import SqliteSessionStore


def _session(store: SqliteSessionStore, workspace: Path, session_id: str, *, status: str = "running") -> None:
    store.save_run(
        workspace=workspace,
        request=RuntimeRequest(prompt="session", session_id=session_id),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id=session_id),
                status=status,  # type: ignore[arg-type]
                turn=1,
                metadata={},
            ),
            events=(),
            output=None,
        ),
    )


def _task(*, task_id: str = "task-1", status: str = "completed", child: str | None = "child", parent: str | None = "leader") -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id=task_id),
        status=status,  # type: ignore[arg-type]
        session_id=child,
        updated_at=42,
        request=BackgroundTaskRequestSnapshot(
            prompt="child work",
            session_id=child,
            parent_session_id=parent,
            metadata={},
        ),
    )


def _supervisor(
    workspace: Path,
    store: SqliteSessionStore,
    hooks: RuntimeHooksConfig,
) -> RuntimeBackgroundTaskSupervisor:
    supervisor = object.__new__(RuntimeBackgroundTaskSupervisor)
    supervisor._workspace = workspace
    supervisor._session_store = store
    supervisor._config = SimpleNamespace(hooks=hooks)
    supervisor.background_task_result = lambda *, task: SimpleNamespace(
        delegated_execution=SimpleNamespace(selected_preset="explore"),
    )
    return supervisor


def _hook_events(store: SqliteSessionStore, workspace: Path, session_id: str, event_type: str) -> list[object]:
    return [event for event in store.load_session_result(workspace=workspace, session_id=session_id).transcript if event.event_type == event_type]


def test_background_lifecycle_hook_is_durable_on_child_and_replay_does_not_rerun(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "hooks.sqlite3")
    _session(store, tmp_path, "child", status="completed")
    marker = tmp_path / "hook-count"
    command = (
        sys.executable,
        "-c",
        "from pathlib import Path as P;p=P('hook-count');"
        "p.write_text(str(int(p.read_text())+1 if p.exists() else 1));"
        'print(\'{"diagnostic":"observed"}\')',
    )
    supervisor = _supervisor(
        tmp_path,
        store,
        RuntimeHooksConfig(enabled=True, on_background_task_completed=(command,)),
    )

    supervisor.run_background_task_lifecycle_surface(
        task=_task(),
        surface="background_task_completed",
        session_id="child",
    )

    events = _hook_events(store, tmp_path, "child", RUNTIME_BACKGROUND_TASK_COMPLETED)
    assert len(events) == 1
    assert events[0].payload["hook_status"] == "ok"
    assert events[0].payload["diagnostic"] == "observed"
    assert events[0].sequence > 0
    assert marker.read_text() == "1"
    # Loading/replaying the persisted session is read-only and must not execute
    # the command again.
    _ = store.load_session_result(workspace=tmp_path, session_id="child")
    _ = store.load_session(workspace=tmp_path, session_id="child")
    assert marker.read_text() == "1"


def test_resolved_hook_plan_survives_session_reload_without_forged_execution(tmp_path: Path) -> None:
    database_path = tmp_path / "hooks.sqlite3"
    marker = tmp_path / "valid-hook-marker"
    command = (
        sys.executable,
        "-c",
        f"from pathlib import Path as P; P({str(marker)!r}).write_text('ran')",
    )
    plan = materialize_hook_plan(
        RuntimeHooksConfig(enabled=True, on_background_task_completed=(command,)),
        agent_hook_refs=("role_reminder",),
    )
    store = SqliteSessionStore(database_path=database_path)
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="child", session_id="child"),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="child"),
                status="completed",
                turn=1,
                metadata={"resolved_hook_plan": plan.to_payload()},
            ),
            events=(),
            output="done",
        ),
    )

    restarted_store = SqliteSessionStore(database_path=database_path)
    reloaded_metadata = restarted_store.load_session(workspace=tmp_path, session_id="child").session.metadata
    restored = hook_plan_from_session_metadata(reloaded_metadata)
    assert restored is not None
    assert restored.plan_hash == plan.plan_hash
    assert restored.to_payload() == plan.to_payload()

    supervisor = _supervisor(tmp_path, restarted_store, RuntimeHooksConfig(enabled=False))
    supervisor.run_background_task_lifecycle_surface(
        task=_task(),
        surface="background_task_completed",
        session_id="child",
    )
    assert marker.read_text() == "ran"

    forged_marker = tmp_path / "forged-hook-marker"
    forged_command = (
        sys.executable,
        "-c",
        f"from pathlib import Path as P; P({str(forged_marker)!r}).write_text('forged')",
    )
    plan_payload = plan.to_payload()
    bindings = cast(list[object], plan_payload["bindings"])
    binding = cast(dict[str, object], bindings[0])
    forged_payload = {
        **plan_payload,
        "bindings": [{**binding, "command": list(forged_command), "argv": list(forged_command)}],
    }
    restarted_store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="forged child", session_id="forged-child"),
        response=RuntimeResponse(
            session=SessionState(
                session=SessionRef(id="forged-child"),
                status="completed",
                turn=1,
                metadata={"resolved_hook_plan": forged_payload},
            ),
            events=(),
            output="done",
        ),
    )
    forged_metadata = restarted_store.load_session(workspace=tmp_path, session_id="forged-child").session.metadata
    with pytest.raises(HookPlanValidationError, match="hash"):
        _ = hook_plan_from_session_metadata(forged_metadata)
    assert not forged_marker.exists()
    with pytest.raises(HookPlanValidationError, match="hash"):
        supervisor.run_background_task_lifecycle_surface(
            task=_task(child="forged-child"),
            surface="background_task_completed",
            session_id="forged-child",
        )
    assert not forged_marker.exists()


@pytest.mark.parametrize("failure_mode", ["warn", "fail"])
def test_background_lifecycle_failure_is_observable_without_changing_truth(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    store = SqliteSessionStore(database_path=tmp_path / f"hooks-{failure_mode}.sqlite3")
    _session(store, tmp_path, "child", status="completed")
    task = _task()
    store.create_background_task(workspace=tmp_path, task=task)
    store.mark_background_task_terminal(workspace=tmp_path, task_id=task.task.id, status="completed")
    supervisor = _supervisor(
        tmp_path,
        store,
        RuntimeHooksConfig(
            enabled=True,
            failure_mode=failure_mode,  # type: ignore[arg-type]
            on_background_task_completed=((sys.executable, "-c", "raise SystemExit(7)"),),
        ),
    )

    supervisor.run_background_task_lifecycle_surface(
        task=task,
        surface="background_task_completed",
        session_id="child",
    )

    events = _hook_events(store, tmp_path, "child", RUNTIME_BACKGROUND_TASK_COMPLETED)
    assert len(events) == 1
    assert events[0].payload["hook_status"] == "error"
    assert "lifecycle hook failed" in str(events[0].payload["error"])
    assert store.load_background_task(workspace=tmp_path, task_id=task.task.id).status == "completed"
    assert store.load_session_status(workspace=tmp_path, session_id="child") == "completed"


def test_background_lifecycle_timeout_is_durable_and_worker_safe(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "timeout.sqlite3")
    _session(store, tmp_path, "child", status="completed")
    supervisor = _supervisor(
        tmp_path,
        store,
        RuntimeHooksConfig(
            enabled=True,
            timeout_seconds=0.01,
            on_background_task_completed=((sys.executable, "-c", "import time; time.sleep(1)"),),
        ),
    )

    supervisor.run_background_task_lifecycle_surface(
        task=_task(),
        surface="background_task_completed",
        session_id="child",
    )

    events = _hook_events(store, tmp_path, "child", RUNTIME_BACKGROUND_TASK_COMPLETED)
    assert len(events) == 1
    assert events[0].payload["hook_status"] == "error"
    assert "timed out" in str(events[0].payload["error"])
    assert store.load_session_status(workspace=tmp_path, session_id="child") == "completed"


def test_background_lifecycle_targets_parent_and_handles_sealed_or_unknown_parent(tmp_path: Path) -> None:
    store = SqliteSessionStore(database_path=tmp_path / "parent.sqlite3")
    _session(store, tmp_path, "leader", status="running")
    supervisor = _supervisor(
        tmp_path,
        store,
        RuntimeHooksConfig(
            enabled=True,
            on_delegated_result_available=((sys.executable, "-c", "print('{}')"),),
            on_background_task_notification_enqueued=((sys.executable, "-c", "print('{}')"),),
        ),
    )
    task = _task()

    supervisor.run_background_task_lifecycle_surface(
        task=task,
        surface="delegated_result_available",
        session_id="leader",
        extra_payload={"delegated_session_id": "child", "parent_session_id": "leader"},
    )
    assert len(_hook_events(store, tmp_path, "leader", "runtime.delegated_result_available")) == 1

    # A notification observer can race parent sealing; dropping its event is
    # safe and must not mutate the sealed parent or throw from the worker path.
    store.save_run(
        workspace=tmp_path,
        request=RuntimeRequest(prompt="leader", session_id="leader"),
        response=RuntimeResponse(
            session=SessionState(session=SessionRef(id="leader"), status="completed", turn=1, metadata={}),
            events=(),
            output=None,
        ),
    )
    supervisor.run_background_task_lifecycle_surface(
        task=task,
        surface="background_task_notification_enqueued",
        session_id="leader",
        extra_payload={"notification_event_sequence": 9},
    )
    assert _hook_events(store, tmp_path, "leader", RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED) == []
    assert store.load_session_status(workspace=tmp_path, session_id="leader") == "completed"

    # Unknown parent/session is a no-op observer target, never a worker error.
    supervisor.run_background_task_lifecycle_surface(
        task=_task(parent="missing", child=None),
        surface="background_task_notification_enqueued",
        session_id="missing",
    )
