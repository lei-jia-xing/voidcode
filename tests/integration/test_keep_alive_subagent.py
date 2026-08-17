"""Integration tests for the keep-alive subagent lifecycle (Phase 3).

Covers the observable contracts of the keep-alive delegated worker
(design doc ``docs/keep-alive-subagent-design.md`` section 4/5):

- intermediate keep-alive turns park the task ``idle`` (awaiting steer)
  without the one-shot ``submit_result`` requirement;
- ``steer_background_task`` dispatches a new worker turn on the *same*
  child session, and the child transcript accumulates across turns;
- the final steer turn that calls ``submit_result`` completes the task and
  repairs the child session row to ``completed``;
- cancelling an idle keep-alive task marks it ``cancelled`` while the child
  session stays resumable;
- runtime shutdown parks an idle keep-alive task ``interrupted`` with the
  child session and transcript preserved, and a fresh runtime (process
  restart) can steer the same task id (``interrupted -> running``);
- the one-shot child ``submit_result`` contract is unchanged: a delegated
  child whose turn carries no ``keep_alive_turn`` metadata still raises
  ``ValueError`` when it completes without ``submit_result``.

The tests run the deterministic graph engine (no provider, no network) with
a prompt-driven graph that performs one tool call per child turn.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pytest

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
        session_store: object | None = None,
    ) -> RuntimeRunner: ...


class ToolCallFactory(Protocol):
    def __call__(self, *, tool_name: str, arguments: dict[str, object]) -> object: ...


class ToolResultLike(Protocol):
    tool_name: str
    content: str
    status: str


class ContextSegmentLike(Protocol):
    role: str
    content: object
    tool_name: str | None


class AssembledContextLike(Protocol):
    prompt: str
    segments: tuple[ContextSegmentLike, ...]
    tool_results: tuple[ToolResultLike, ...]
    metadata: dict[str, object]


class ProviderRequestLike(Protocol):
    assembled_context: AssembledContextLike
    available_tools: tuple[object, ...]


def _assembled_context(request: object) -> AssembledContextLike:
    return cast(ProviderRequestLike, request).assembled_context


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)
    runtime_class = cast(RuntimeFactory, service_module.VoidCodeRuntime)
    return runtime_request, runtime_class


@dataclass(frozen=True, slots=True)
class _GraphStep:
    events: tuple[object, ...] = ()
    tool_call: object | None = None
    output: str | None = None
    is_finished: bool = False


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
    ``submit_result`` produces the transcript handoff that completes the
    task. Every child request is recorded so tests can assert that later
    turns rehydrate the accumulated transcript.
    """

    def __init__(self, child_requests: list[object]) -> None:
        self._child_requests = child_requests

    def step(
        self,
        request: object,
        tool_results: tuple[object, ...],
        *,
        session: object,
    ) -> _GraphStep:
        session_ref = cast(SessionLike, session).session
        prompt = _assembled_context(request).prompt
        if getattr(session_ref, "parent_id", None) is None:
            if not tool_results:
                return _GraphStep(
                    tool_call=_tool_call(
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
                )
            return _GraphStep(
                output=cast(ToolResultLike, tool_results[-1]).content,
                is_finished=True,
            )
        self._child_requests.append(request)
        tool_names = [cast(ToolResultLike, result).tool_name for result in tool_results]
        if "submit_result" in prompt and "submit_result" not in tool_names:
            return _GraphStep(
                tool_call=_tool_call(
                    tool_name="submit_result",
                    arguments={
                        "summary": "final keep-alive handoff",
                        "completed_work": ["wrote second.txt"],
                    },
                ),
            )
        if "write second.txt" in prompt and "write" not in tool_names:
            return _GraphStep(
                tool_call=_tool_call(
                    tool_name="write",
                    arguments={"path": "second.txt", "content": "second marker"},
                ),
            )
        if "read sample.txt" in prompt and "read" not in tool_names:
            return _GraphStep(
                tool_call=_tool_call(
                    tool_name="read",
                    arguments={"path": "sample.txt"},
                ),
            )
        return _GraphStep(
            output=cast(ToolResultLike, tool_results[-1]).content,
            is_finished=True,
        )


class _ImmediateFinishGraph:
    """Graph that finishes every turn immediately (no tool calls)."""

    def step(
        self,
        request: object,
        tool_results: tuple[object, ...],
        *,
        session: object,
    ) -> _GraphStep:
        _ = request, tool_results
        session_ref = cast(SessionLike, session).session
        if getattr(session_ref, "parent_id", None) is not None:
            return _GraphStep(output="child done without handoff", is_finished=True)
        return _GraphStep(output="leader done", is_finished=True)


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
        parts.append(result.content or "")
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
                graph=_KeepAliveChildGraph(child_requests),
                permission_policy=permission_policy(mode="allow"),
            ),
        ),
    )
    return runtime_request, runtime


def test_keep_alive_intermediate_turn_parks_idle_without_submit_result(tmp_path: Path) -> None:
    """A keep-alive intermediate turn completes without submit_result.

    The run loop must skip the one-shot ``delegated child must call
    submit_result`` check when the internal ``keep_alive_turn`` metadata is
    set; the child parks ``interrupted`` (resumable) and the task parks
    ``idle`` (awaiting steer). The parent session receives the
    ``runtime.background_task_awaiting_steer`` event even though its row is
    already sealed.
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
    assert "delegated child must call submit_result" not in _event_text(child.transcript)

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
    prior prompts and their outputs). The final steer's ``submit_result``
    completes the task and repairs the child session row to ``completed``.
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
    final_steer = runtime.steer_background_task(task_id, "final submit_result handoff")
    assert final_steer.status == "running"
    completed = _wait_for_background_task_status(runtime, task_id, {"completed"})

    assert completed.status == "completed"
    assert completed.keep_alive is True
    assert completed.error is None

    # The final turn's assembled context rehydrated BOTH prior turns: the
    # prompts and their tool outputs accumulated in the child transcript.
    final_turn_context = _context_text(_first_child_request_with_prompt(child_requests, "final submit_result handoff"))
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
                graph=_KeepAliveChildGraph(child_requests),
                permission_policy=permission_policy(mode="allow"),
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


def test_keep_alive_one_shot_child_submit_result_contract_unchanged(tmp_path: Path) -> None:
    """Regression: the one-shot child contract is byte-for-byte unchanged.

    A delegated child turn WITHOUT the internal ``keep_alive_turn`` metadata
    still raises ``ValueError`` when it completes without ``submit_result`` —
    the run loop gating must not leak into non-keep-alive children.
    """
    runtime_request, runtime_class = _load_runtime_types()
    runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, graph=_ImmediateFinishGraph())),
    )
    leader = runtime.run(runtime_request(prompt="leader", session_id="leader-session"))
    assert leader.session.status == "completed"

    with pytest.raises(ValueError, match="delegated child must call submit_result"):
        runtime.run(
            runtime_request(
                prompt="child turn without handoff",
                session_id="child-session",
                parent_session_id="leader-session",
            )
        )
