"""Unit tests for the unified ``background_task`` steer operation (keep-alive Phase 3).

Covers runtime supervisor lifecycle transitions and the facade's parent-session check.
"""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast
from unittest.mock import patch

import pytest

from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.tools.contracts import ToolCall
from voidcode.tools.delegation.background_task import BackgroundTaskTool
from voidcode.tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context

pytestmark = pytest.mark.usefixtures("force_deterministic_engine_default")


@pytest.fixture
def force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    config_module = importlib.import_module("voidcode.runtime.config")
    monkeypatch.setattr(
        config_module,
        "_default_runtime_mcp_config",
        lambda: config_module.RuntimeMcpConfig(enabled=False),
    )
    monkeypatch.setattr(config_module, "_default_runtime_mcp_servers", lambda: {})


class RuntimeRequestLike(Protocol):
    prompt: str
    metadata: dict[str, object]


class RuntimeRequestFactory(Protocol):
    def __call__(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> RuntimeRequestLike: ...


class BackgroundTaskRefLike(Protocol):
    id: str


class BackgroundTaskStateLike(Protocol):
    task: BackgroundTaskRefLike
    status: str
    session_id: str | None
    error: str | None
    cancel_requested_at: int | None
    keep_alive: bool
    steer_prompt: str | None
    observability: object | None


class RuntimeLike(Protocol):
    def start_background_task(self, request: RuntimeRequestLike) -> BackgroundTaskStateLike: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskStateLike: ...

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskStateLike: ...

    def shutdown_background_tasks(self, *, timeout_seconds: float = 2.0) -> None: ...


def _runtime(tmp_path: Path) -> RuntimeLike:
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime = cast(RuntimeLike, service_module.VoidCodeRuntime(workspace=tmp_path))
    return runtime


def _runtime_request_factory() -> RuntimeRequestFactory:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    return cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)


def _wait_for_background_task_status(
    runtime: RuntimeLike,
    task_id: str,
    statuses: set[str],
    *,
    timeout: float = 5.0,
) -> BackgroundTaskStateLike:
    deadline = time.monotonic() + timeout
    last_task: BackgroundTaskStateLike | None = None
    while time.monotonic() < deadline:
        task = runtime.load_background_task(task_id)
        last_task = task
        if task.status in statuses:
            return task
        time.sleep(0.01)
    if last_task is None:
        detail = "last_status=None"
    else:
        detail = f"last_status={last_task.status!r} error={last_task.error!r}"
    raise AssertionError(f"background task {task_id} did not reach {sorted(statuses)}; {detail}")


def _idle_keep_alive_task(tmp_path: Path, runtime: RuntimeLike) -> BackgroundTaskStateLike:
    """Create a keep-alive task and wait until its first turn parks it idle."""
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    runtime_request = _runtime_request_factory()
    task = runtime.start_background_task(runtime_request(prompt="read sample.txt", metadata={"keep_alive": True}))
    return _wait_for_background_task_status(runtime, task.task.id, {"idle"})


def test_steer_background_task_rejects_non_keep_alive_task(tmp_path: Path) -> None:
    """A completed one-shot task is not keep-alive and cannot be steered."""
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    runtime_request = _runtime_request_factory()
    task = runtime.start_background_task(runtime_request(prompt="read sample.txt"))
    terminal = _wait_for_background_task_status(
        runtime,
        task.task.id,
        {"completed", "failed", "cancelled", "interrupted"},
    )
    assert terminal.keep_alive is False

    with pytest.raises(ValueError, match="is not a keep-alive task and cannot be steered"):
        runtime.steer_background_task(task.task.id, "read sample.txt")


def test_steer_background_task_rejects_empty_content(tmp_path: Path) -> None:
    """Steer content must be a non-empty string (whitespace-only rejected)."""
    runtime = _runtime(tmp_path)
    idle_task = _idle_keep_alive_task(tmp_path, runtime)
    assert idle_task.status == "idle"

    with pytest.raises(ValueError, match="steer requires non-empty content"):
        runtime.steer_background_task(idle_task.task.id, "")
    with pytest.raises(ValueError, match="steer requires non-empty content"):
        runtime.steer_background_task(idle_task.task.id, "   ")


def test_steer_background_task_rejects_running_turn_in_flight(tmp_path: Path) -> None:
    """A task with a turn in flight (running) cannot be steered (no pipelining)."""
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    runtime_request = _runtime_request_factory()
    read_module = importlib.import_module("voidcode.tools.read")
    read_tool = read_module.ReadTool
    original_invoke = read_tool.invoke
    started = threading.Event()
    release = threading.Event()

    def _blocking_read(self: object, call: object, *, workspace: Path) -> object:
        started.set()
        _ = release.wait(timeout=2)
        return original_invoke(self, call, workspace=workspace)

    with patch.object(read_tool, "invoke", autospec=True, side_effect=_blocking_read):
        task = runtime.start_background_task(runtime_request(prompt="read sample.txt", metadata={"keep_alive": True}))
        assert started.wait(timeout=1) is True
        running = runtime.load_background_task(task.task.id)
        assert running.status == "running"

        with pytest.raises(ValueError, match="can only be steered while idle or interrupted"):
            runtime.steer_background_task(task.task.id, "read sample.txt")

        release.set()
        idle = _wait_for_background_task_status(runtime, task.task.id, {"idle"})

    assert idle.status == "idle"


def test_concurrent_steers_launch_only_one_worker(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    idle_task = _idle_keep_alive_task(tmp_path, runtime)
    supervisor = cast(Any, runtime)._background_task_supervisor
    first_spawn_entered = threading.Event()
    release_first_spawn = threading.Event()
    spawn_calls: list[str] = []

    def fake_spawn(*, task_id: str, reserved_identity: object) -> tuple[object, threading.Event]:
        _ = reserved_identity
        spawn_calls.append(task_id)
        if len(spawn_calls) == 1:
            first_spawn_entered.set()
            assert release_first_spawn.wait(timeout=2)
        return SimpleNamespace(start=lambda: None), threading.Event()

    outcomes: list[object] = []

    def steer() -> None:
        try:
            outcomes.append(runtime.steer_background_task(idle_task.task.id, "continue"))
        except ValueError as exc:
            outcomes.append(exc)

    with patch.object(supervisor, "_spawn_worker_thread", side_effect=fake_spawn):
        first = threading.Thread(target=steer)
        second = threading.Thread(target=steer)
        first.start()
        assert first_spawn_entered.wait(timeout=1)
        second.start()
        release_first_spawn.set()
        first.join(timeout=2)
        second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert spawn_calls == [idle_task.task.id]
    assert len(outcomes) == 2
    assert sum(isinstance(outcome, ValueError) for outcome in outcomes) == 1


def test_steer_background_task_idle_dispatch_runs_turn_and_parks_idle(tmp_path: Path) -> None:
    """Steering an idle keep-alive task flips to running with the steer prompt.

    The dispatched turn runs on the same child session and parks the task
    back ``idle`` (no terminal yield), clearing the ``steer_prompt`` column.
    """
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    runtime = _runtime(tmp_path)
    idle_task = _idle_keep_alive_task(tmp_path, runtime)
    child_session_id = idle_task.session_id
    assert child_session_id is not None

    steered = runtime.steer_background_task(idle_task.task.id, "read sample.txt")

    assert steered.status == "running"
    assert steered.steer_prompt == "read sample.txt"
    assert steered.keep_alive is True
    assert steered.session_id == child_session_id

    idle_again = _wait_for_background_task_status(runtime, idle_task.task.id, {"idle"})
    assert idle_again.status == "idle"
    assert idle_again.steer_prompt is None
    assert idle_again.session_id == child_session_id


def test_steer_background_task_interrupted_keep_alive_resumes_same_task(tmp_path: Path) -> None:
    """An interrupted keep-alive task (shutdown breakpoint) resumes on steer.

    After a runtime shutdown parks the task ``interrupted``, a fresh runtime
    (process restart) steers the SAME task id: the row flips
    ``interrupted -> running`` and the same child session is re-entered.
    """
    runtime = _runtime(tmp_path)
    idle_task = _idle_keep_alive_task(tmp_path, runtime)
    task_id = idle_task.task.id
    child_session_id = idle_task.session_id
    assert child_session_id is not None

    runtime.shutdown_background_tasks()
    parked = runtime.load_background_task(task_id)
    assert parked.status == "interrupted"
    assert "runtime exited while keep-alive worker was awaiting steer" in (parked.error or "")

    fresh = _runtime(tmp_path)
    steered = fresh.steer_background_task(task_id, "read sample.txt")

    assert steered.status == "running"
    assert steered.keep_alive is True
    assert steered.session_id == child_session_id

    idle_again = _wait_for_background_task_status(fresh, task_id, {"idle"})
    assert idle_again.status == "idle"


class _StubSteerRuntime:
    def __init__(self, task: BackgroundTaskState) -> None:
        self._task = task
        self.calls: list[tuple[str, str]] = []
        self.steered: list[tuple[str, str]] = []

    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None:
        self.calls.append(("authorize", task_id))
        if parent_session_id != self._task.parent_session_id:
            raise ValueError("only its parent session may steer it")

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        self.calls.append(("load", task_id))
        assert task_id == self._task.task.id
        return self._task

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState:
        self.calls.append(("steer", task_id))
        self.steered.append((task_id, content))
        return BackgroundTaskState(
            task=self._task.task,
            status="running",
            request=self._task.request,
            session_id=self._task.session_id,
            keep_alive=True,
            steer_prompt=content,
        )


def _steerable_task(*, parent_session_id: str) -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id="task-keep-alive-1"),
        status="idle",
        request=BackgroundTaskRequestSnapshot(
            prompt="read sample.txt",
            parent_session_id=parent_session_id,
            metadata={"keep_alive": True},
        ),
        session_id="child-session-1",
        keep_alive=True,
    )


def test_background_task_steer_rejects_non_parent_session() -> None:
    """Only the task's parent session may steer it via the background_task facade."""
    runtime = _StubSteerRuntime(task=_steerable_task(parent_session_id="parent-1"))
    tool = BackgroundTaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="other-session")):
        with pytest.raises(ValueError, match="only its parent"):
            tool.invoke(
                ToolCall(
                    tool_name="background_task",
                    arguments={"operation": "steer", "task_id": "task-keep-alive-1", "prompt": "continue"},
                ),
                workspace=Path("."),
            )

    assert runtime.steered == []
    assert runtime.calls == [("authorize", "task-keep-alive-1")]


def test_background_task_steer_parent_dispatches_steer() -> None:
    """The task's parent session can steer it; the facade surfaces the dispatch."""
    runtime = _StubSteerRuntime(task=_steerable_task(parent_session_id="parent-1"))
    tool = BackgroundTaskTool(runtime=runtime)

    with bind_runtime_tool_context(RuntimeToolInvocationContext(session_id="parent-1")):
        result = tool.invoke(
            ToolCall(
                tool_name="background_task",
                arguments={"operation": "steer", "task_id": "task-keep-alive-1", "prompt": "continue"},
            ),
            workspace=Path("."),
        )

    assert runtime.calls == [
        ("authorize", "task-keep-alive-1"),
        ("load", "task-keep-alive-1"),
        ("steer", "task-keep-alive-1"),
    ]
    assert result.status == "ok"
    assert result.data["task_id"] == "task-keep-alive-1"
    assert result.data["status"] == "running"
    assert result.data["keep_alive"] is True
    assert result.data["steer_prompt"] == "continue"
    assert result.data["child_session_id"] == "child-session-1"
    assert result.data["terminal"] is False
    assert runtime.steered == [("task-keep-alive-1", "continue")]
