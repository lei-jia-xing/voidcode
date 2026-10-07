"""Integration tests for the keep-alive subagent lifecycle (Phase 3).

Covers the observable contracts of the keep-alive delegated worker:

- intermediate keep-alive turns park the task ``idle`` (awaiting steer)
  without the one-shot ``yield`` requirement;
- ``steer_background_task`` dispatches a new worker turn on the *same*
  child session, and the child transcript accumulates across turns;
- the final steer turn that calls ``yield`` completes the task and
  repairs the child session row to ``completed``;
- cancelling an idle keep-alive task marks it ``cancelled`` while the child
  session stays resumable;
- runtime shutdown parks an idle keep-alive task ``interrupted`` with the
  child session and transcript preserved, and a fresh runtime (process
  restart) can steer the same task id (``interrupted -> running``);
- runtime shutdown that expires its wait seizes execution ownership: the
  still-running worker cannot commit a completion, change task state, append a
  parent notification, or write child-session events afterwards, the refusal is
  recorded as a diagnostic, and a later steer resumes the task under a NEW
  execution that does commit;
- the one-shot child ``yield`` contract is enforced: a delegated child
  whose turn carries no ``keep_alive_turn`` metadata still raises
  ``ValueError`` when it completes without ``yield``.

The tests run the deterministic graph engine (no provider, no network) with
a prompt-driven graph that performs one tool call per child turn.
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from voidcode.core.transcript import ToolResultView, tool_result_output
from voidcode.core.turns import FinalTurn, ToolTurn, TurnSession
from voidcode.runtime.execution_ownership import EXECUTION_OWNERSHIP
from voidcode.runtime.storage import SessionRepository

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


class EventLike(Protocol):
    event_type: str
    payload: dict[str, object]
    sequence: int


class SessionLike(Protocol):
    session: SessionRefLike
    status: str
    metadata: dict[str, object]


class SessionRefLike(Protocol):
    id: str
    parent_id: str | None


class RuntimeResponseLike(Protocol):
    events: tuple[EventLike, ...]
    output: str | None
    session: SessionLike
    transcript: tuple[EventLike, ...]


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


class BackgroundTaskObservabilityLike(Protocol):
    waiting_reason: str


class BackgroundTaskStateLike(Protocol):
    task: BackgroundTaskRefLike
    status: str
    session_id: str | None
    error: str | None
    cancel_requested_at: int | None
    keep_alive: bool
    steer_prompt: str | None
    observability: BackgroundTaskObservabilityLike | None


class RuntimeRunner(Protocol):
    def run(self, request: RuntimeRequestLike) -> RuntimeResponseLike: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskStateLike: ...

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskStateLike: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskStateLike: ...

    def session_result(self, *, session_id: str) -> RuntimeResponseLike: ...

    def shutdown_background_tasks(self, *, timeout_seconds: float = 2.0) -> None: ...


class RuntimeFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        tool_registry: object | None = None,
        graph: object | None = None,
        config: object | None = None,
        mcp_manager: object | None = None,
        permission_policy: object | None = None,
        repositories: object | None = None,
    ) -> RuntimeRunner: ...


class ToolCallFactory(Protocol):
    def __call__(self, *, tool_name: str, arguments: dict[str, object]) -> object: ...


class ContextSegmentLike(Protocol):
    role: str
    content: object
    tool_name: str | None


class AssembledContextLike(Protocol):
    prompt: str
    segments: tuple[ContextSegmentLike, ...]
    tool_results: tuple[ToolResultView, ...]
    metadata: dict[str, object]


class ProviderRequestLike(Protocol):
    assembled_context: AssembledContextLike
    available_tools: tuple[object, ...]


def _assembled_context(request: object) -> AssembledContextLike:
    return cast(ProviderRequestLike, request).assembled_context


def _last_tool_output(tool_results: tuple[object, ...]) -> str:
    output = tool_result_output(cast(ToolResultView, tool_results[-1]))
    if output is None:
        raise AssertionError("the final delegated tool result has no text presentation")
    return output


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)
    runtime_class = cast(RuntimeFactory, service_module.VoidCodeRuntime)
    return runtime_request, runtime_class


def _tool_call(*, tool_name: str, arguments: dict[str, object]) -> object:
    return cast(ToolCallFactory, importlib.import_module("voidcode.tools.contracts").ToolCall)(
        tool_name=tool_name,
        arguments=arguments,
    )


class _KeepAliveChildGraph:
    """Prompt-driven graph for the keep-alive lifecycle.

    Leader branch: delegates a keep-alive background child via the ``task``
    tool and finishes after the tool result. Child branch: performs exactly
    one tool call per turn, selected by the turn prompt (the steer content),
    then finishes the turn. Intermediate keep-alive turns therefore park the
    child ``interrupted`` and the task ``idle``; the final turn's
    ``yield`` produces the transcript handoff that completes the
    task. Every child request is recorded so tests can assert that later
    turns rehydrate the accumulated transcript.
    """

    def __init__(self, child_requests: list[object]) -> None:
        self._child_requests = child_requests

    def produce(
        self,
        request: object,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        prompt = _assembled_context(request).prompt
        if session.metadata.get("parent_session_id") is None:
            if not tool_results:
                return ToolTurn(
                    calls=(
                        _tool_call(
                            tool_name="task",
                            arguments={
                                "prompt": "read sample.txt",
                                "run_in_background": True,
                                "load_skills": [],
                                "subagent_type": "worker",
                                "description": "Keep-alive child",
                                "keep_alive": True,
                            },
                        ),
                    ),
                )
            return FinalTurn(output=_last_tool_output(tool_results))
        self._child_requests.append(request)
        tool_names = [cast(ToolResultView, result).tool_name for result in tool_results]
        if "yield" in prompt and "yield" not in tool_names:
            return ToolTurn(
                calls=(
                    _tool_call(
                        tool_name="yield",
                        arguments={
                            "summary": "final keep-alive handoff",
                            "data": {"completed_work": ["wrote second.txt"]},
                        },
                    ),
                ),
            )
        if "write second.txt" in prompt and "write" not in tool_names:
            return ToolTurn(
                calls=(
                    _tool_call(
                        tool_name="write",
                        arguments={"path": "second.txt", "content": "second marker"},
                    ),
                ),
            )
        if "read sample.txt" in prompt and "read" not in tool_names:
            return ToolTurn(
                calls=(
                    _tool_call(
                        tool_name="read",
                        arguments={"path": "sample.txt"},
                    ),
                ),
            )
        return FinalTurn(output=_last_tool_output(tool_results))


class _ImmediateFinishGraph:
    """Graph that finishes every turn immediately (no tool calls)."""

    def produce(
        self,
        request: object,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        _ = request, tool_results
        if session.metadata.get("parent_session_id") is not None:
            return FinalTurn(output="child done without handoff")
        return FinalTurn(output="leader done")


class _BlockingKeepAliveChildGraph:
    """Leader delegates a keep-alive child whose first turns block on gates.

    ``blocks[i]`` is the ``(entered, gate)`` pair for child turn ``i + 1``: the
    turn signals ``entered`` and waits on ``gate`` before submitting its
    ``yield``. Turns without a pair submit immediately. A blocked turn provably
    outlives a short shutdown wait — the audited window in which a seized
    worker used to complete anyway.
    """

    def __init__(self, *, blocks: tuple[tuple[threading.Event, threading.Event], ...] = ()) -> None:
        self.blocks = blocks
        self._turn_index = 0

    def produce(
        self,
        request: object,
        tool_results: tuple[object, ...],
        *,
        session: TurnSession,
    ) -> ToolTurn | FinalTurn:
        if session.metadata.get("parent_session_id") is None:
            if not tool_results:
                return ToolTurn(
                    calls=(
                        _tool_call(
                            tool_name="task",
                            arguments={
                                "prompt": "blocking keep-alive child",
                                "run_in_background": True,
                                "load_skills": [],
                                "subagent_type": "worker",
                                "description": "Blocking keep-alive child",
                                "keep_alive": True,
                            },
                        ),
                    ),
                )
            return FinalTurn(output=_last_tool_output(tool_results))
        if any(cast(ToolResultView, result).tool_name == "yield" for result in tool_results):
            return FinalTurn(output="child finished")
        self._turn_index += 1
        if self._turn_index <= len(self.blocks):
            entered, gate = self.blocks[self._turn_index - 1]
            entered.set()
            gate.wait(timeout=15.0)
        return ToolTurn(
            calls=(
                _tool_call(
                    tool_name="yield",
                    arguments={"summary": "keep-alive handoff", "data": {"completed_work": ["child turn"]}},
                ),
            ),
        )


class BackgroundTaskSupervisorLike(Protocol):
    threads: dict[str, threading.Thread]


@dataclass(frozen=True, slots=True)
class _SeizedKeepAlive:
    """Observable result of seizing a blocked keep-alive execution."""

    task_id: str
    child_session_id: str
    worker: threading.Thread
    task_after_seizure: BackgroundTaskStateLike
    child_after_seizure: dict[str, object]
    parent_after_seizure: dict[str, object]


def _runtime_internals(runtime: RuntimeRunner) -> Any:
    """Runtime internals the observable contract does not expose (test-only)."""
    return cast(Any, runtime)


def _supervisor(runtime: RuntimeRunner) -> BackgroundTaskSupervisorLike:
    return cast(BackgroundTaskSupervisorLike, _runtime_internals(runtime)._background_task_supervisor)


def _session_ledger(runtime: RuntimeRunner, workspace: Path, session_id: str) -> dict[str, object]:
    """Observable session truth: status, event count, watermark, completions.

    Read from the store rather than ``session_result``: a delegated child that
    is still mid-turn has no ``agent_capability_snapshot`` yet, and the whole
    point of the seized assertion is to read the row *while* the run is
    unfinished.
    """
    store = cast(SessionRepository, _runtime_internals(runtime)._repositories.sessions)
    events = store.load_session(workspace=workspace, session_id=session_id).events
    return {
        "status": store.load_session_status(workspace=workspace, session_id=session_id),
        "event_count": len(events),
        "max_sequence": max((event.sequence for event in events), default=0),
        "completion_notifications": sum(1 for event in events if event.event_type == "runtime.background_task_completed"),
    }


def _blocking_keep_alive_runtime(
    tmp_path: Path,
    *,
    blocks: tuple[tuple[threading.Event, threading.Event], ...] = (),
) -> tuple[RuntimeRequestFactory, RuntimeRunner]:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                turn_producer=_BlockingKeepAliveChildGraph(blocks=blocks),
                permission_policy=permission_policy(mode="yolo"),
            ),
        ),
    )
    return runtime_request, runtime


def _seize_blocked_keep_alive_child(tmp_path: Path) -> tuple[RuntimeRunner, _SeizedKeepAlive]:
    """Run the audited scenario: seize a keep-alive worker that is still blocked.

    Deterministic throughout: the child signals ``entered`` then blocks on
    ``gate``; the shutdown wait is allowed to expire; the worker thread is
    released and joined before anything is asserted about the outcome.
    """
    entered: threading.Event = threading.Event()
    gate: threading.Event = threading.Event()
    runtime_request, runtime = _blocking_keep_alive_runtime(tmp_path, blocks=((entered, gate),))

    leader = runtime.run(runtime_request(prompt="delegate blocking keep-alive child", session_id="leader-session"))
    task_id = _task_id_from_leader_run(leader)
    assert entered.wait(timeout=10.0), "child never entered its blocking step"

    worker = _supervisor(runtime).threads[task_id]
    child_session_id = runtime.load_background_task(task_id).session_id
    assert child_session_id is not None

    runtime.shutdown_background_tasks(timeout_seconds=0.05)
    task_after_seizure = runtime.load_background_task(task_id)
    # The wait expired while the execution was still running: without an
    # ownership rule this worker commits a completion after shutdown returned.
    assert worker.is_alive(), "the blocked child did not outlive the shutdown wait"
    seized = _SeizedKeepAlive(
        task_id=task_id,
        child_session_id=child_session_id,
        worker=worker,
        task_after_seizure=task_after_seizure,
        child_after_seizure=_session_ledger(runtime, tmp_path, child_session_id),
        parent_after_seizure=_session_ledger(runtime, tmp_path, "leader-session"),
    )
    gate.set()
    worker.join(timeout=10.0)
    assert not worker.is_alive(), "the seized worker did not exit after the gate was released"
    return runtime, seized


def test_keep_alive_shutdown_seizes_worker_ownership_and_refuses_late_completion(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A seized keep-alive execution cannot commit anything after shutdown returned.

    Shutdown expires its wait while the child turn is still blocked; the worker
    threads that fact through as an ownership revocation, so the late
    completion it produces when the block clears is refused everywhere:
    task state, child-session events, and the parent completion notification.
    The refusal itself is preserved as a diagnostic.
    """
    with caplog.at_level(logging.WARNING, logger="voidcode.runtime.execution_ownership"):
        runtime, seized = _seize_blocked_keep_alive_child(tmp_path)

    assert seized.task_after_seizure.status == "interrupted"
    final_task = runtime.load_background_task(seized.task_id)

    # (b) task state: the seized execution cannot flip ``interrupted`` to a
    # completion, and it cannot clear the shutdown error it does not own.
    assert final_task.status == "interrupted"
    assert final_task.error == seized.task_after_seizure.error
    assert final_task.error == "runtime exited during keep-alive worker turn"

    # (d) child-session truth: no event and no row upgrade from the revoked run.
    assert _session_ledger(runtime, tmp_path, seized.child_session_id) == seized.child_after_seizure

    # (c) no parent notification from the revoked execution.
    assert _session_ledger(runtime, tmp_path, "leader-session") == seized.parent_after_seizure
    assert _session_ledger(runtime, tmp_path, "leader-session")["completion_notifications"] == 0

    # (a)+(3) the refused late completion is recorded, not lost.
    diagnostics = [diagnostic for diagnostic in EXECUTION_OWNERSHIP.late_writes() if diagnostic.task_id == seized.task_id]
    assert diagnostics, "the late write after revocation was not recorded"
    assert diagnostics[0].reason == "runtime shutdown deadline expired while the execution was in flight"
    assert any("lost ownership" in record.getMessage() for record in caplog.records)


def test_keep_alive_interrupted_resume_runs_under_a_new_execution_and_commits(tmp_path: Path) -> None:
    """``interrupted`` stays resumable: the next steer owns the task and commits.

    A late completion from the seized worker is not a resume. Resuming is a
    runtime-owned transition that grants a NEW execution; that execution must
    still be able to complete the task and repair the child session row.
    """
    _, seized = _seize_blocked_keep_alive_child(tmp_path)

    # A fresh runtime (process restart) is how ``interrupted`` is resumed: the
    # old runtime's shutdown flag is terminal for its own dispatcher.
    settled = threading.Event()
    settled.set()
    _, fresh_runtime = _blocking_keep_alive_runtime(tmp_path, blocks=((settled, settled),))

    steered = fresh_runtime.steer_background_task(seized.task_id, "resume handoff")
    assert steered.status == "running"
    assert steered.session_id == seized.child_session_id

    completed = _wait_for_background_task_status(fresh_runtime, seized.task_id, {"completed"})
    assert completed.error is None

    child = fresh_runtime.session_result(session_id=seized.child_session_id)
    assert child.session.status == "completed"
    assert _session_ledger(fresh_runtime, tmp_path, "leader-session")["completion_notifications"] == 1


def test_keep_alive_resume_does_not_reauthorize_the_seized_worker(tmp_path: Path) -> None:
    """A new execution's ownership never re-authorizes the seized one.

    Revocation is per lease identity, not per task: after the task is resumed
    (``interrupted -> running`` under a new generation), the old worker's late
    completion is still refused — it neither finishes the task nor becomes the
    resumed turn's result.
    """
    entered_old: threading.Event = threading.Event()
    gate_old: threading.Event = threading.Event()
    runtime_request, runtime = _blocking_keep_alive_runtime(tmp_path, blocks=((entered_old, gate_old),))
    leader = runtime.run(runtime_request(prompt="delegate blocking keep-alive child", session_id="leader-session"))
    task_id = _task_id_from_leader_run(leader)
    assert entered_old.wait(timeout=10.0), "child never entered its blocking step"
    worker_old = _supervisor(runtime).threads[task_id]

    runtime.shutdown_background_tasks(timeout_seconds=0.05)
    assert worker_old.is_alive(), "the blocked child did not outlive the shutdown wait"

    # Resume: a new execution takes ownership of the same task and blocks.
    entered_new: threading.Event = threading.Event()
    gate_new: threading.Event = threading.Event()
    _, fresh_runtime = _blocking_keep_alive_runtime(tmp_path, blocks=((entered_new, gate_new),))
    steered = fresh_runtime.steer_background_task(task_id, "resume handoff")
    assert steered.status == "running"
    child_session_id = steered.session_id
    assert child_session_id is not None
    assert entered_new.wait(timeout=10.0), "the resumed execution never entered its blocking step"
    live_lease = EXECUTION_OWNERSHIP.current(workspace=tmp_path, task_id=task_id)
    assert live_lease is not None
    assert live_lease.generation == 2, "the resumed execution must own a NEW generation"

    # Release the OLD seized worker while the new execution is still running.
    gate_old.set()
    worker_old.join(timeout=10.0)
    assert not worker_old.is_alive(), "the seized worker did not exit after the gate was released"
    assert fresh_runtime.load_background_task(task_id).status == "running"

    gate_new.set()
    completed = _wait_for_background_task_status(fresh_runtime, task_id, {"completed"})
    assert completed.error is None
    assert fresh_runtime.session_result(session_id=child_session_id).session.status == "completed"
    # Exactly one completion notification: the new execution's.
    assert _session_ledger(fresh_runtime, tmp_path, "leader-session")["completion_notifications"] == 1
    refusals = [diagnostic for diagnostic in EXECUTION_OWNERSHIP.late_writes() if diagnostic.task_id == task_id]
    assert refusals, "the seized worker's late write was not recorded"
    assert {diagnostic.generation for diagnostic in refusals} == {1}


def _wait_for_background_task_status(
    runtime: RuntimeRunner,
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


def _context_text(request: object) -> str:
    assembled = _assembled_context(request)
    parts: list[str] = [assembled.prompt]
    for segment in assembled.segments:
        content = segment.content
        if isinstance(content, str):
            parts.append(content)
    for result in assembled.tool_results:
        parts.append(tool_result_output(result) or "")
    return "\n".join(parts)


def _first_child_request_with_prompt(
    child_requests: list[object],
    needle: str,
) -> object:
    for request in child_requests:
        if needle in _assembled_context(request).prompt:
            return request
    raise AssertionError(f"no child request carried prompt containing {needle!r}")


def _event_text(events: tuple[EventLike, ...]) -> str:
    parts: list[str] = []
    for event in events:
        parts.append(event.event_type)
        parts.append(str(event.payload))
    return "\n".join(parts)


def _task_id_from_leader_run(response: RuntimeResponseLike) -> str:
    task_completed = next(event for event in response.events if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "task")
    task_id = task_completed.payload.get("task_id")
    if not isinstance(task_id, str):
        raise AssertionError(f"task tool completed event carried no task_id: {task_completed.payload}")
    return task_id


def _keep_alive_runtime(
    tmp_path: Path,
    child_requests: list[object],
) -> tuple[RuntimeRequestFactory, RuntimeRunner]:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                turn_producer=_KeepAliveChildGraph(child_requests),
                permission_policy=permission_policy(mode="yolo"),
            ),
        ),
    )
    return runtime_request, runtime


def test_keep_alive_intermediate_turn_parks_idle_without_yield(tmp_path: Path) -> None:
    """A keep-alive intermediate turn completes without yield.

    The run loop must skip the one-shot ``delegated child must call yield``
    check when the internal ``keep_alive_turn`` metadata is set; the child
    parks ``interrupted`` (resumable) and the task parks ``idle``.
    """
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    runtime_request, runtime = _keep_alive_runtime(tmp_path, child_requests=[])

    response = runtime.run(runtime_request(prompt="delegate keep-alive child", session_id="leader-session"))
    task_id = _task_id_from_leader_run(response)
    idle_task = _wait_for_background_task_status(runtime, task_id, {"idle"})

    assert response.session.status == "completed"
    assert idle_task.status == "idle"
    assert idle_task.error is None
    assert idle_task.keep_alive is True
    assert idle_task.observability is not None
    assert idle_task.observability.waiting_reason == "awaiting_steer"
    assert idle_task.steer_prompt is None

    child_session_id = idle_task.session_id
    assert child_session_id is not None
    child = runtime.session_result(session_id=child_session_id)
    assert child.session.status == "interrupted"

    # The one-shot error never fired: it would have failed the task or the
    # leader turn.
    assert "delegated child must call yield" not in _event_text(child.transcript)

    deadline = time.monotonic() + 3.0
    awaiting_steer = None
    while time.monotonic() < deadline:
        leader = runtime.session_result(session_id="leader-session")
        awaiting_steer = next(
            (
                event
                for event in leader.transcript
                if event.event_type == "runtime.background_task_awaiting_steer" and event.payload.get("task_id") == task_id
            ),
            None,
        )
        if awaiting_steer is not None:
            break
        time.sleep(0.01)
    assert awaiting_steer is not None
    assert awaiting_steer.payload["status"] == "idle"
    assert awaiting_steer.payload["child_session_id"] == child_session_id


def test_keep_alive_two_steers_accumulate_child_transcript_then_final_turn_completes(tmp_path: Path) -> None:
    """Full lifecycle: task -> idle -> steer -> idle -> steer(final) -> completed.

    Each steer dispatches a fresh worker turn on the same child session; the
    next turn's assembled context rehydrates the accumulated transcript (both
    prompts and their outputs). The final steer's ``yield`` completes the
    task and repairs the child session row to ``completed``.
    """
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    child_requests: list[object] = []
    runtime_request, runtime = _keep_alive_runtime(tmp_path, child_requests)

    response = runtime.run(runtime_request(prompt="delegate keep-alive child", session_id="leader-session"))
    task_id = _task_id_from_leader_run(response)
    idle_task = _wait_for_background_task_status(runtime, task_id, {"idle"})
    child_session_id = idle_task.session_id
    assert child_session_id is not None

    # Steer 1: the worker runs a fresh turn on the SAME child session.
    steered = runtime.steer_background_task(task_id, "write second.txt second marker")
    assert steered.status == "running"
    assert steered.steer_prompt == "write second.txt second marker"
    assert steered.session_id == child_session_id

    idle_again = _wait_for_background_task_status(runtime, task_id, {"idle"})
    assert idle_again.status == "idle"
    assert idle_again.steer_prompt is None  # cleared when the turn parks idle
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second marker"

    # Steer 2 (final): the child submits its handoff.
    final_steer = runtime.steer_background_task(task_id, "final yield handoff")
    assert final_steer.status == "running"
    completed = _wait_for_background_task_status(runtime, task_id, {"completed"})

    assert completed.status == "completed"
    assert completed.keep_alive is True
    assert completed.error is None

    # The final turn's assembled context rehydrated BOTH prior turns: the
    # prompts and their tool outputs accumulated in the child transcript.
    final_turn_context = _context_text(_first_child_request_with_prompt(child_requests, "final yield handoff"))
    assert "read sample.txt" in final_turn_context
    assert "Read 1 line(s) from sample.txt." in final_turn_context
    assert "write second.txt second marker" in final_turn_context
    assert "Wrote file successfully: second.txt" in final_turn_context

    # The finalize repaired the child session row from interrupted to
    # completed (the transcript handoff was durable evidence).
    child = runtime.session_result(session_id=child_session_id)
    assert child.session.status == "completed"


def test_keep_alive_idle_cancel_terminalizes_task_cancelled(tmp_path: Path) -> None:
    """Cancelling an idle keep-alive task marks it cancelled.

    An idle task owns no worker thread, so the cancel must terminalize the
    row directly; the child session stays interrupted/resumable.
    """
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    runtime_request, runtime = _keep_alive_runtime(tmp_path, child_requests=[])

    response = runtime.run(runtime_request(prompt="delegate keep-alive child", session_id="leader-session"))
    task_id = _task_id_from_leader_run(response)
    idle_task = _wait_for_background_task_status(runtime, task_id, {"idle"})
    child_session_id = idle_task.session_id

    cancelled = runtime.cancel_background_task(task_id)

    assert cancelled.status == "cancelled"
    assert cancelled.error == "cancelled by parent while awaiting steer"
    assert cancelled.cancel_requested_at is None
    child = runtime.session_result(session_id=child_session_id)
    assert child.session.status == "interrupted"


def test_keep_alive_shutdown_parks_interrupted_and_fresh_runtime_resumes_same_task(tmp_path: Path) -> None:
    """Shutdown parks an idle keep-alive task interrupted, child preserved.

    Keep-alive is a process-lifetime concept: after shutdown (or a crash) the
    task is terminalized ``interrupted`` while the child session and full
    transcript stay intact. A fresh runtime can steer the SAME task id
    (``interrupted -> running`` breakpoint resume) and the re-entered child
    session still carries the accumulated transcript.
    """
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    child_requests: list[object] = []
    runtime_request, runtime = _keep_alive_runtime(tmp_path, child_requests)

    response = runtime.run(runtime_request(prompt="delegate keep-alive child", session_id="leader-session"))
    task_id = _task_id_from_leader_run(response)
    idle_task = _wait_for_background_task_status(runtime, task_id, {"idle"})
    child_session_id = idle_task.session_id
    assert child_session_id is not None

    runtime.shutdown_background_tasks()
    parked = runtime.load_background_task(task_id)
    assert parked.status == "interrupted"
    assert "runtime exited while keep-alive worker was awaiting steer" in (parked.error or "")

    # The child session and its transcript survived the shutdown.
    child = runtime.session_result(session_id=child_session_id)
    assert child.session.status == "interrupted"
    assert "Read 1 line(s) from sample.txt." in _event_text(child.transcript)

    # A fresh runtime (process restart) resumes the same task id on the same
    # child session; the steered turn rehydrates the prior transcript.
    _, fresh = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    fresh_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            fresh(
                workspace=tmp_path,
                turn_producer=_KeepAliveChildGraph(child_requests),
                permission_policy=permission_policy(mode="yolo"),
            ),
        ),
    )
    steered = fresh_runtime.steer_background_task(task_id, "write second.txt second marker")
    assert steered.status == "running"
    assert steered.session_id == child_session_id

    idle_again = _wait_for_background_task_status(fresh_runtime, task_id, {"idle"})
    assert idle_again.status == "idle"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second marker"

    resumed_context = _context_text(_first_child_request_with_prompt(child_requests, "write second.txt second marker"))
    assert "read sample.txt" in resumed_context
    assert "Read 1 line(s) from sample.txt." in resumed_context


def test_keep_alive_one_shot_child_yield_contract(tmp_path: Path) -> None:
    """A one-shot child still requires terminal yield.

    A delegated child turn without ``keep_alive_turn`` must fail when it
    completes without yield.
    """
    runtime_request, runtime_class = _load_runtime_types()
    runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=_ImmediateFinishGraph())),
    )
    leader = runtime.run(runtime_request(prompt="leader", session_id="leader-session"))
    assert leader.session.status == "completed"

    with pytest.raises(ValueError, match="delegated child must call yield"):
        runtime.run(
            runtime_request(
                prompt="child turn without handoff",
                session_id="child-session",
                parent_session_id="leader-session",
            )
        )
