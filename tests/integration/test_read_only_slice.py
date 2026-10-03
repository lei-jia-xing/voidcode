"""Integration tests for the deterministic read-only slice."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from voidcode.core.turns import TurnPlan, TurnSession
from voidcode.runtime.paths import sessions_db_path
from voidcode.runtime.storage import RuntimeRepositories, SqliteSessionStore

pytestmark = pytest.mark.usefixtures("force_deterministic_engine_default")

_DEFAULT_PERMISSION_METADATA = {
    "external_directory_read": {"*": "allow"},
    "external_directory_write": {"*": "ask"},
}
_LEADER_HOOK_PRESET_SNAPSHOT = {
    "refs": [
        "role_reminder",
        "delegation_guard",
        "background_output_quality_guidance",
        "delegated_retry_guidance",
        "todo_continuation_guidance",
    ],
    "kinds": ["guidance", "guard", "guidance", "guard", "continuation"],
    "event_scopes": [
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.background_task_cancelled",
        "runtime.background_task_completed",
        "runtime.background_task_failed",
        "runtime.background_task_interrupted",
        "runtime.background_task_result_read",
        "runtime.delegated_result_available",
        "runtime.permission_resolved",
        "runtime.request_received",
        "runtime.stuck_detected",
        "runtime.todo_updated",
        "runtime.tool_started",
        "runtime.turn_progress",
    ],
    "allowed_actions": ["cancel", "guidance", "observe", "report"],
    "authority": "non_authoritative",
    "source": "builtin",
    "count": 5,
}


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


def _cwd_command() -> str:
    return f'"{sys.executable}" -c "import os; print(os.getcwd())"'


class EventLike(Protocol):
    event_type: str
    payload: dict[str, object]
    sequence: int


class StreamChunkLike(Protocol):
    kind: str
    session: SessionLike
    event: EventLike | None
    output: str | None


class SessionLike(Protocol):
    session: SessionRefLike
    status: str
    metadata: dict[str, object]


class SessionRefLike(Protocol):
    id: str
    parent_id: str | None


class StoredSessionSummaryLike(Protocol):
    session: SessionRefLike
    status: str
    turn: int
    prompt: str
    updated_at: int


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


class BackgroundTaskStateLike(Protocol):
    task: BackgroundTaskRefLike
    status: str
    session_id: str | None
    error: str | None
    cancel_requested_at: int | None


class StoredBackgroundTaskSummaryLike(Protocol):
    task: BackgroundTaskRefLike
    status: str
    prompt: str


class RuntimeRunner(Protocol):
    def run(self, request: RuntimeRequestLike) -> RuntimeResponseLike: ...

    def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]: ...

    def resume_stream(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: str | None = None,
    ) -> Iterator[StreamChunkLike]: ...

    def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]: ...

    def session_result(self, *, session_id: str) -> RuntimeResponseLike: ...

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: str | None = None,
    ) -> RuntimeResponseLike: ...

    def start_background_task(self, request: RuntimeRequestLike) -> BackgroundTaskStateLike: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskStateLike: ...

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummaryLike, ...]: ...

    def list_background_tasks_by_parent_session(self, *, parent_session_id: str) -> tuple[StoredBackgroundTaskSummaryLike, ...]: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskStateLike: ...


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


class ToolResultLike(Protocol):
    tool_name: str
    content: str
    data: dict[str, object]
    reference: str | None


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


def _assembled_context(request: object) -> AssembledContextLike:
    return cast(ProviderRequestLike, request).assembled_context


class EventEnvelopeFactory(Protocol):
    def __call__(
        self,
        *,
        session_id: str,
        sequence: int,
        event_type: str,
        source: str,
        payload: dict[str, object] | None = None,
    ) -> object: ...


class ReadToolType(Protocol):
    invoke: Callable[..., object]


class ToolRegistryLike(Protocol):
    tools: dict[str, object]

    def excluding(self, tool_names: Iterable[str]) -> ToolRegistryLike: ...


class ToolRegistryClassLike(Protocol):
    def with_defaults(self) -> ToolRegistryLike: ...


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)
    runtime_class = cast(RuntimeFactory, service_module.VoidCodeRuntime)
    return runtime_request, runtime_class


class _NoopMcpManager:
    def current_state(self) -> object:
        mcp_module = importlib.import_module("voidcode.runtime.mcp")
        return mcp_module.McpManagerState(
            mode="managed",
            configuration=mcp_module.McpConfigState(configured_enabled=True),
        )

    def list_tools(self, **_: object) -> tuple[object, ...]:
        return ()

    def call_tool(self, **_: object) -> object:
        raise AssertionError("MCP tool calls are not used by this test")

    def drain_events(self) -> tuple[object, ...]:
        return ()

    def shutdown(self) -> tuple[object, ...]:
        return ()


class _DefaultConfiguredMcpManager(_NoopMcpManager):
    def current_state(self) -> object:
        mcp_module = importlib.import_module("voidcode.runtime.mcp")
        return mcp_module.McpManagerState(
            mode="managed",
            configuration=mcp_module.McpConfigState(
                configured_enabled=True,
                servers={"context7": object(), "websearch": object(), "grep_app": object()},
            ),
        )

    def list_tools(self, **_: object) -> tuple[object, ...]:
        mcp_types = importlib.import_module("voidcode.mcp.types")
        return (
            mcp_types.McpToolDescriptor(
                server_name="context7",
                tool_name="query-docs",
                description="Query docs",
                input_schema={"type": "object"},
                safety=mcp_types.McpToolSafety(read_only=True),
            ),
        )


class _AstGrepPreviewGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if not tool_results:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name="ast_grep",
                        arguments={
                            "mode": "preview",
                            "pattern": "print($X)",
                            "rewrite": "logger.info($X)",
                            "path": "sample.py",
                            "lang": "python",
                        },
                    ),
                ),
            )
        return TurnPlan(facts=(), tool_calls=(), output="previewed", is_finished=True)


class _AstGrepReplaceGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if not tool_results:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name="ast_grep",
                        arguments={
                            "mode": "replace",
                            "pattern": "print($X)",
                            "rewrite": "logger.info($X)",
                            "path": "sample.py",
                            "lang": "python",
                            "apply": True,
                        },
                    ),
                ),
            )
        return TurnPlan(facts=(), tool_calls=(), output="applied", is_finished=True)


class _SingleToolGraph:
    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self._tool_name = tool_name
        self._arguments = arguments

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if not tool_results:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name=self._tool_name,
                        arguments=self._arguments,
                    ),
                ),
            )
        return TurnPlan(facts=(), tool_calls=(), output="done", is_finished=True)


class _SequentialToolGraph:
    def __init__(self, calls: tuple[tuple[str, dict[str, object]], ...]) -> None:
        self._calls = calls

    def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if len(tool_results) < len(self._calls):
            tool_name, arguments = self._calls[len(tool_results)]
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name=tool_name,
                        arguments=arguments,
                    ),
                ),
            )
        return TurnPlan(facts=(), tool_calls=(), output="done", is_finished=True)


class _SequentialSafeBoundaryGraph(_SequentialToolGraph):
    """Sequential tool graph that reports every step as a safe boundary.

    Unlike the plain ``_SequentialToolGraph``, this exposes
    ``is_at_safe_boundary()`` so the run loop captures an ``interrupted``
    checkpoint after each completed tool call. A crash between tool calls
    therefore resumes from the last completed tool rather than re-running
    the whole loop from scratch.
    """

    def is_at_safe_boundary(self) -> bool:
        return True


def _approval_runtime(
    tmp_path: Path,
    *,
    mode: str = "ask",
    graph: object | None = None,
) -> tuple[RuntimeRequestFactory, RuntimeRunner]:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    policy = permission_policy(mode=mode)
    runtime_kwargs: dict[str, object] = {
        "workspace": tmp_path,
        "permission_policy": policy,
        "mcp_manager": _NoopMcpManager(),
    }
    if graph is not None:
        runtime_kwargs["graph"] = graph
    runtime = cast(RuntimeRunner, cast(object, runtime_class(**runtime_kwargs)))
    return runtime_request, runtime


@dataclass(frozen=True, slots=True)
class _ScriptedModelProvider:
    name: str
    outcomes: tuple[object, ...]

    def turn_provider(self) -> object:
        outcomes = list(self.outcomes)
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                _ = request
                if not outcomes:
                    return importlib.import_module("voidcode.provider.protocol").ProviderTurnResult(output="done")
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return _Provider()


@dataclass(frozen=True, slots=True)
class _CapturingModelProvider:
    name: str
    requests: list[object]

    def turn_provider(self) -> object:
        requests = self.requests
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                requests.append(request)
                return importlib.import_module("voidcode.provider.protocol").ProviderTurnResult(output="done")

        return _Provider()


@dataclass(frozen=True, slots=True)
class _ReadFileParityModelProvider:
    name: str
    requests: list[object]

    def turn_provider(self) -> object:
        requests = self.requests
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                requests.append(request)
                provider_protocol_module = importlib.import_module("voidcode.provider.protocol")
                tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
                if not _assembled_context(request).tool_results:
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="read",
                            arguments={"path": "sample.txt"},
                            tool_call_id="read-1",
                        )
                    )
                return provider_protocol_module.ProviderTurnResult(output="done")

        return _Provider()


class _SingleThenBatchTurnProvider:
    """Scripted provider that returns one call, then an authentic two-call batch.

    The batch is restored from the runtime's durable continuation after a crash,
    so the next provider request must follow all three completed results.
    """

    name = "opencode-zen"

    def __init__(self) -> None:
        self.propose_turn_tool_result_counts: list[int] = []

    def propose_turn(self, request: object) -> object:
        provider_protocol_module = importlib.import_module("voidcode.provider.protocol")
        tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
        tool_results = _assembled_context(request).tool_results
        self.propose_turn_tool_result_counts.append(len(tool_results))
        if not tool_results:
            return provider_protocol_module.ProviderTurnResult(
                tool_call=tool_contracts_module.ToolCall(
                    tool_name="read",
                    arguments={"path": "a.txt"},
                    tool_call_id="call-a",
                )
            )
        if len(tool_results) == 1:
            return provider_protocol_module.ProviderTurnResult(
                tool_calls=(
                    tool_contracts_module.ToolCall(
                        tool_name="read",
                        arguments={"path": "b.txt"},
                        tool_call_id="call-b",
                    ),
                    tool_contracts_module.ToolCall(
                        tool_name="read",
                        arguments={"path": "c.txt"},
                        tool_call_id="call-c",
                    ),
                )
            )
        return provider_protocol_module.ProviderTurnResult(output="done")


@dataclass(frozen=True, slots=True)
class _DelegationE2EModelProvider:
    name: str

    def turn_provider(self) -> object:
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                provider_protocol_module = importlib.import_module("voidcode.provider.protocol")
                tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
                assembled_context = _assembled_context(request)
                tool_results = assembled_context.tool_results
                if _is_delegated_child_request(request):
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="yield",
                            arguments={"summary": "child final", "data": {"completed_work": ["returned delegated result"]}},
                        )
                    )
                if not tool_results:
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="task",
                            arguments={
                                "prompt": "return the child final",
                                "run_in_background": False,
                                "load_skills": [],
                                "subagent_type": "explore",
                                "description": "Sync subagent E2E child",
                            },
                        )
                    )
                return provider_protocol_module.ProviderTurnResult(output="parent continued after child final")

        return _Provider()


@dataclass(frozen=True, slots=True)
class _ParentToolResultGuardrailProvider:
    name: str
    requests: list[object]

    def turn_provider(self) -> object:
        requests = self.requests
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                requests.append(request)
                provider_protocol_module = importlib.import_module("voidcode.provider.protocol")
                tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
                assembled_context = _assembled_context(request)
                tool_results = assembled_context.tool_results
                if _is_delegated_child_request(request):
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="yield",
                            arguments={"summary": "child clean"},
                        )
                    )
                if not tool_results:
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="read",
                            arguments={"path": "parent-secret.txt"},
                        )
                    )
                if len(tool_results) == 1:
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="task",
                            arguments={
                                "prompt": "check child isolation",
                                "run_in_background": False,
                                "load_skills": [],
                                "subagent_type": "explore",
                                "description": "Context isolation child",
                            },
                        )
                    )
                return provider_protocol_module.ProviderTurnResult(output="parent done")

        return _Provider()


@dataclass(frozen=True, slots=True)
class _BackgroundOutputGuardrailProvider:
    name: str
    requests: list[object]

    def turn_provider(self) -> object:
        requests = self.requests
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                requests.append(request)
                provider_protocol_module = importlib.import_module("voidcode.provider.protocol")
                tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
                assembled_context = _assembled_context(request)
                tool_results = assembled_context.tool_results
                if _is_delegated_child_request(request):
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="yield",
                            arguments={"summary": "child transcript sentinel"},
                        )
                    )
                if not tool_results:
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="task",
                            arguments={
                                "prompt": "produce child transcript sentinel",
                                "run_in_background": True,
                                "load_skills": [],
                                "subagent_type": "explore",
                                "description": "Background transcript child",
                            },
                        )
                    )
                if len(tool_results) == 1:
                    task_id = cast(str, tool_results[0].data["task_id"])
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="task",
                            arguments={
                                "operation": "output",
                                "task_id": task_id,
                                "block": True,
                                "timeout": 3000,
                                "full_session": True,
                                "message_limit": 10,
                            },
                        )
                    )
                return provider_protocol_module.ProviderTurnResult(output="parent collected transcript")

        return _Provider()


def _is_delegated_child_request(request: object) -> bool:
    assembled_context = _assembled_context(request)
    metadata = assembled_context.metadata
    if not isinstance(metadata, dict):
        return False
    delegation = metadata.get("delegation")
    return isinstance(delegation, dict)


def _wait_for_background_task_status(
    runtime: RuntimeRunner,
    task_id: str,
    statuses: set[str],
    *,
    timeout: float = 3.0,
) -> BackgroundTaskStateLike:
    deadline = time.monotonic() + timeout
    last_task: BackgroundTaskStateLike | None = None
    while time.monotonic() < deadline:
        task = runtime.load_background_task(task_id)
        last_task = task
        if task.status in statuses:
            return task
        time.sleep(0.01)
    raise AssertionError(
        f"background task {task_id} did not reach {sorted(statuses)}; last_status={last_task.status if last_task is not None else None!r}"
    )


def _assert_ordered_event_types(actual: Iterable[str], expected: Iterable[str]) -> None:
    remaining = iter(actual)
    for expected_type in expected:
        for event_type in remaining:
            if event_type == expected_type:
                break
        else:
            raise AssertionError(f"missing ordered event type: {expected_type}")


def _write_demo_skill(skill_dir: Path, *, content: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: Demo skill\n---\n{content}\n",
        encoding="utf-8",
    )


class _ParentBackgroundOutputGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> object:
        _ = request
        if session.metadata.get("parent_session_id") is not None:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(ToolCallFactory, importlib.import_module("voidcode.tools.contracts").ToolCall)(
                        tool_name="yield",
                        arguments={"summary": "child background final"},
                    ),
                ),
            )
        tool_call_factory = cast(
            ToolCallFactory,
            importlib.import_module("voidcode.tools.contracts").ToolCall,
        )
        if not tool_results:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    tool_call_factory(
                        tool_name="task",
                        arguments={
                            "prompt": "finish in the background",
                            "run_in_background": True,
                            "load_skills": [],
                            "subagent_type": "explore",
                            "description": "Background E2E child",
                        },
                    ),
                ),
            )
        first_result = cast(ToolResultLike, tool_results[0])
        first_data = first_result.data
        if len(tool_results) == 1:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    tool_call_factory(
                        tool_name="task",
                        arguments={
                            "operation": "output",
                            "task_id": first_data["task_id"],
                            "block": True,
                            "timeout": 3000,
                            "full_session": True,
                        },
                    ),
                ),
            )
        final_result = cast(ToolResultLike, tool_results[1])
        return TurnPlan(facts=(), tool_calls=(), output=final_result.content, is_finished=True)


class _FailingBackgroundChildGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> object:
        _ = request, tool_results
        if session.metadata.get("parent_session_id") is not None:
            raise RuntimeError("delegated child failed twice")
        return TurnPlan(facts=(), tool_calls=(), output="leader ready", is_finished=True)


class _McpEchoGraph:
    def produce(self, request: object, tool_results: tuple[object, ...], *, session: TurnSession) -> object:
        _ = request, session
        if not tool_results:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name="mcp/echo/echo",
                        arguments={"text": "delegated mcp"},
                    ),
                ),
            )
        if session.metadata.get("parent_session_id") is not None:
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(ToolCallFactory, importlib.import_module("voidcode.tools.contracts").ToolCall)(
                        tool_name="yield",
                        arguments={"summary": "mcp child done", "data": {"completed_work": ["called delegated MCP"]}},
                    ),
                ),
            )
        return TurnPlan(facts=(), tool_calls=(), output="mcp parent done", is_finished=True)


def test_runtime_background_restart_reconcile_reloads_terminal_delegated_result(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    first_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=_ParentBackgroundOutputGraph())),
    )
    _ = first_runtime.run(runtime_request(prompt="leader", session_id="leader-restart"))
    started = first_runtime.start_background_task(
        runtime_request(
            prompt="restart child",
            parent_session_id="leader-restart",
            metadata={"delegation": {"mode": "background", "subagent_type": "explore"}},
        )
    )
    completed = _wait_for_background_task_status(first_runtime, started.task.id, {"completed"})

    second_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=_ParentBackgroundOutputGraph())),
    )
    reloaded = second_runtime.load_background_task(started.task.id)
    task_result = cast(Any, second_runtime).load_background_task_result(started.task.id)

    assert completed.status == "completed"
    assert reloaded.status == "completed"
    assert task_result.status == "completed"
    assert task_result.summary_output == "child background final"
    assert task_result.result_available is True


def test_runtime_skips_hooks_for_nested_hook_launched_runtime_invocations(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    src_path = str(Path(__file__).resolve().parents[2] / "src")
    marker_path = tmp_path / "hook-count.txt"
    nested_output_path = tmp_path / "nested-hook-output.txt"
    (tmp_path / "nested.txt").write_text("nested hook read\n", encoding="utf-8")

    hook_script = "\n".join(
        [
            "import os",
            "import subprocess",
            "import sys",
            "from pathlib import Path",
            f"workspace = Path({str(tmp_path)!r})",
            f"marker_path = workspace / {marker_path.name!r}",
            f"nested_output_path = workspace / {nested_output_path.name!r}",
            "count = int(marker_path.read_text(encoding='utf-8')) if marker_path.exists() else 0",
            "marker_path.write_text(str(count + 1), encoding='utf-8')",
            "if count == 0:",
            "    env = dict(os.environ)",
            f"    src_path = {src_path!r}",
            "    existing_pythonpath = env.get('PYTHONPATH')",
            "    env['PYTHONPATH'] = (",
            "        src_path",
            "        if not existing_pythonpath",
            "        else f'{src_path}{os.pathsep}{existing_pythonpath}'",
            "    )",
            "    result = subprocess.run(",
            "        [",
            "            sys.executable,",
            "            '-m',",
            "            'voidcode',",
            "            'run',",
            "            'read nested.txt',",
            "            '--workspace',",
            "            str(workspace),",
            "            '--session-id',",
            "            'nested-hook-session',",
            "        ],",
            "        cwd=workspace,",
            "        capture_output=True,",
            "        text=True,",
            "        check=True,",
            "        env=env,",
            "    )",
            "    nested_output_path.write_text(result.stdout, encoding='utf-8')",
        ]
    )
    (tmp_path / ".voidcode.json").write_text(
        json.dumps(
            {
                "approval_mode": "yolo",
                "hooks": {
                    "enabled": True,
                    "timeout_seconds": 90,
                    "pre_tool": [[sys.executable, "-c", hook_script]],
                },
            }
        ),
        encoding="utf-8",
    )

    command = _cwd_command()
    prompt = f"run {command}"
    runtime = runtime_class(workspace=tmp_path)
    result = runtime.run(runtime_request(prompt=prompt, session_id="outer-hook-recursion-session"))

    assert marker_path.read_text(encoding="utf-8") == "1"
    nested_output = nested_output_path.read_text(encoding="utf-8")
    assert nested_output == "nested hook read\n"
    assert "runtime.tool_hook_pre" not in nested_output
    assert "runtime.tool_hook_post" not in nested_output
    assert [event.event_type for event in result.events].count("runtime.tool_hook_pre") == 1


def test_runtime_background_task_persists_and_can_be_loaded_from_fresh_runtime(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()

    first_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    task = first_runtime.start_background_task(runtime_request(prompt="read missing.txt"))

    deadline = time.time() + 2
    terminal_task = None
    while time.time() < deadline:
        current = first_runtime.load_background_task(task.task.id)
        if current.status in ("completed", "failed", "cancelled", "interrupted"):
            terminal_task = current
            break
        time.sleep(0.01)

    assert terminal_task is not None
    second_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    reloaded = second_runtime.load_background_task(task.task.id)
    listed = second_runtime.list_background_tasks()

    assert reloaded.task.id == task.task.id
    assert reloaded.status == terminal_task.status
    assert any(item.task.id == task.task.id for item in listed)
    if reloaded.session_id is not None:
        replay = second_runtime.resume(reloaded.session_id)
        assert replay.session.metadata["background_task_id"] == task.task.id
        assert replay.session.metadata["background_run"] is True


def test_runtime_lists_background_tasks_by_parent_session_from_fresh_runtime(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    _ = (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")

    first_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    _ = first_runtime.run(runtime_request(prompt="read sample.txt", session_id="leader-session"))
    leader_task = first_runtime.start_background_task(runtime_request(prompt="read sample.txt", parent_session_id="leader-session"))
    _ = first_runtime.start_background_task(runtime_request(prompt="read sample.txt"))

    deadline = time.time() + 2
    while time.time() < deadline:
        current = first_runtime.load_background_task(leader_task.task.id)
        if current.status in ("completed", "failed", "cancelled", "interrupted"):
            break
        time.sleep(0.01)

    second_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    listed = second_runtime.list_background_tasks_by_parent_session(parent_session_id="leader-session")

    assert len(listed) == 1
    assert listed[0].task.id == leader_task.task.id
    assert listed[0].prompt == "read sample.txt"


def test_runtime_background_task_cancel_reconciles_orphaned_task_from_fresh_runtime(
    tmp_path: Path,
) -> None:
    _, runtime_class = _load_runtime_types()
    task_module = importlib.import_module("voidcode.runtime.background.models")
    storage_module = importlib.import_module("voidcode.runtime.storage")

    first_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    _ = first_runtime
    store = cast(SqliteSessionStore, storage_module.SqliteSessionStore())
    store.create_background_task(
        workspace=tmp_path,
        task=task_module.BackgroundTaskState(
            task=task_module.BackgroundTaskRef(id="task-fresh-cancel"),
            request=task_module.BackgroundTaskRequestSnapshot(prompt="read sample.txt"),
            created_at=1,
            updated_at=1,
        ),
    )

    second_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    cancelled = second_runtime.cancel_background_task("task-fresh-cancel")

    assert cancelled.status == "cancelled"
    assert cancelled.error == "cancelled before start"
    assert cancelled.cancel_requested_at is None


def test_runtime_persists_pending_approval_until_single_resume_resolution(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")

    waiting = runtime.run(runtime_request(prompt="write danger.txt persisted approval", session_id="persisted-approval"))
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    _, replay_runtime_class = _load_runtime_types()
    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            replay_runtime_class(
                workspace=tmp_path,
                permission_policy=policy,
            ),
        ),
    )

    replay = resumed_runtime.resume("persisted-approval")
    resolved = resumed_runtime.resume(
        "persisted-approval",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert replay.session.status == "waiting"
    assert replay.events[-1].event_type == "runtime.approval_requested"
    assert replay.events[-1].payload["policy"] == {"mode": "ask"}
    assert resolved.session.status == "completed"
    with pytest.raises(ValueError, match="no pending approval"):
        _ = resumed_runtime.resume(
            "persisted-approval",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )


def test_runtime_rejects_stale_duplicate_approval_replay_after_resolution_even_if_pending_state_is_restored(  # noqa: E501
    tmp_path: Path,
) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(runtime_request(prompt="write danger.txt stale replay", session_id="stale-replay-session"))
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])
    resolved = runtime.resume(
        "stale-replay-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    database_path = sessions_db_path()
    connection = sqlite3.connect(database_path)
    try:
        approval_event = next(event for event in resolved.events if event.event_type == "runtime.approval_requested")
        _ = connection.execute(
            ("UPDATE sessions SET pending_approval_json = ?, resume_checkpoint_json = ? WHERE session_id = ?"),
            (
                json.dumps(
                    {
                        "request_id": approval_request_id,
                        "tool_name": "write",
                        "arguments": {"path": "danger.txt", "content": "stale replay"},
                        "target_summary": "write danger.txt",
                        "reason": "non-read-only tool invocation",
                        "policy_mode": "ask",
                        "request_event_sequence": approval_event.sequence,
                        "owner_session_id": "stale-replay-session",
                        "owner_parent_session_id": None,
                        "delegated_task_id": None,
                        "path_scope": approval_event.payload["path_scope"],
                        "operation_class": approval_event.payload["operation_class"],
                        "canonical_path": approval_event.payload["canonical_path"],
                        "matched_rule": approval_event.payload["matched_rule"],
                        "policy_surface": approval_event.payload["policy_surface"],
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "version": 1,
                        "kind": "approval_wait",
                        "prompt": "write danger.txt stale replay",
                        "session_status": "waiting",
                        "session_metadata": resolved.session.metadata,
                        "tool_results": [],
                        "last_event_sequence": approval_event.sequence,
                        "pending_approval_request_id": approval_request_id,
                        "output": None,
                    },
                    sort_keys=True,
                ),
                "stale-replay-session",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        ValueError,
        match="approval request was already resolved; stale approval replay is not allowed",
    ):
        _ = runtime.resume(
            "stale-replay-session",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )


class _DivergentWriteFileGraph:
    """Graph that returns different write arguments on consecutive steps."""

    def __init__(self) -> None:
        self._call_count = 0

    def produce(
        self,
        request: object,
        tool_results: tuple[object, ...],
        *,
        session: object,
    ) -> object:
        _ = request, session
        self._call_count += 1
        if not tool_results:
            suffix = "first" if self._call_count == 1 else "second"
            return TurnPlan(
                facts=(),
                tool_calls=(
                    cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name="write",
                        arguments={
                            "path": "divergent.txt",
                            "content": f"body-{suffix}",
                        },
                    ),
                ),
            )
        return TurnPlan(facts=(), tool_calls=(), output="written", is_finished=True)


def test_runtime_resume_uses_persisted_runtime_config_over_fresh_resume_overrides(
    tmp_path: Path,
) -> None:
    provider_config_module = importlib.import_module("voidcode.provider.config")
    # `repo`/`session`/`fresh` are stand-in provider ids marking config precedence; the
    # runtime resolves only ids it knows, so declare them as custom providers.
    standin_providers = provider_config_module.ProviderConfigs(
        custom={provider_name: provider_config_module.ProviderEndpointConfig() for provider_name in ("repo", "session", "fresh")}
    )
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps({"approval_mode": "ask", "model": "repo/model", "providers": {"custom": {"repo": {}, "session": {}, "fresh": {}}}}),
        encoding="utf-8",
    )
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    load_runtime_config = cast(Callable[..., object], config_module.load_runtime_config)
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)

    initial_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=load_runtime_config(tmp_path, approval_mode="yolo", model="session/model"),
                permission_policy=cast(Callable[..., object], permission_module.PermissionPolicy)(mode="yolo"),
            ),
        ),
    )
    _ = (tmp_path / "sample.txt").write_text("resume config\n", encoding="utf-8")

    _ = initial_runtime.run(runtime_request(prompt="read sample.txt", session_id="resume-config-session"))

    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(approval_mode="ask", model="fresh/model", providers=standin_providers),
                permission_policy=cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask"),
            ),
        ),
    )
    replay = resumed_runtime.resume("resume-config-session")

    assert set(replay.session.metadata) == {
        "workspace",
        "runtime_config",
        "runtime_policy",
        "agent_capability_snapshot",
        "runtime_state",
        "context_window",
        "rulebook_snapshot",
        "selected_skill_names",
        "applied_skills",
        "skill_snapshot",
        "resolved_hook_plan",
    }
    assert replay.session.metadata["runtime_config"] == {
        "approval_mode": "yolo",
        "config_schema_version": 1,
        "execution_engine": "deterministic",
        "fallback_models": [],
        "tool_timeout_seconds": None,
        "lsp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "mcp": {
            "configured_enabled": False,
            "mode": "disabled",
            "servers": [],
        },
        "model": "session/model",
        "permission": _DEFAULT_PERMISSION_METADATA,
        "reminders": {"enabled": True, "todo": {"max_per_cycle": 3}},
        "resolved_provider": {
            "active_target": {
                "raw_model": "session/model",
                "provider": "session",
                "model": "model",
            },
            "targets": [
                {
                    "raw_model": "session/model",
                    "provider": "session",
                    "model": "model",
                }
            ],
        },
    }
    runtime_state = cast(dict[str, object], replay.session.metadata["runtime_state"])
    assert set(runtime_state) == {"acp", "run_id"}
    assert runtime_state["acp"] == {
        "available": False,
        "configured_enabled": False,
        "last_delegation": None,
        "last_error": None,
        "last_event_type": None,
        "last_request_id": None,
        "last_request_type": None,
        "mode": "disabled",
        "status": "disconnected",
    }


def test_runtime_preserves_pending_request_when_resumed_finalize_raises(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")

    waiting = runtime.run(runtime_request(prompt="write danger.txt finalize failure", session_id="approval-session"))
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    class FailingFinalizeGraph:
        def produce(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
            if not tool_results:
                return TurnPlan(
                    facts=(),
                    tool_calls=(
                        cast(
                            ToolCallFactory,
                            importlib.import_module("voidcode.tools.contracts").ToolCall,
                        )(
                            tool_name="write",
                            arguments={"path": "danger.txt", "content": "finalize failure"},
                        ),
                    ),
                )
            raise RuntimeError("finalize boom")

    resumed_runtime_class = _load_runtime_types()[1]
    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            resumed_runtime_class(
                workspace=tmp_path,
                turn_producer=FailingFinalizeGraph(),
                permission_policy=policy,
            ),
        ),
    )
    failed = resumed_runtime.resume(
        "approval-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert failed.session.status == "failed"
    assert failed.events[-1].event_type == "runtime.failed"
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "finalize failure"

    replay_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            _load_runtime_types()[1](
                workspace=tmp_path,
                turn_producer=FailingFinalizeGraph(),
                permission_policy=policy,
            ),
        ),
    )
    replay = replay_runtime.resume("approval-session")

    assert replay.session.status == "failed"
    assert replay.events[-1].event_type == "runtime.failed"


def test_runtime_preserves_pending_approval_when_terminal_save_fails(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")

    waiting = runtime.run(runtime_request(prompt="write danger.txt save failure", session_id="approval-session"))
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    storage_module = importlib.import_module("voidcode.runtime.storage")
    sqlite_store_class = cast(Callable[[], SqliteSessionStore], storage_module.SqliteSessionStore)
    base_store = sqlite_store_class()

    class FailingTerminalSaveStore:
        def save_run(
            self,
            *,
            workspace: Path,
            request: Any,
            response: Any,
            clear_pending_approval: bool = True,
            seal_terminal_status: bool = True,
        ) -> None:
            if clear_pending_approval:
                raise RuntimeError("save boom")
            base_store.save_run(
                workspace=workspace,
                request=request,
                response=response,
                clear_pending_approval=clear_pending_approval,
                seal_terminal_status=seal_terminal_status,
            )

    resumed_runtime_class = _load_runtime_types()[1]
    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            resumed_runtime_class(
                workspace=tmp_path,
                permission_policy=policy,
                repositories=RuntimeRepositories(
                    events=base_store,
                    sessions=base_store,
                    run_writer=FailingTerminalSaveStore(),
                    recovery=base_store,
                    tasks=base_store,
                    maintenance=base_store,
                    process_persistence=base_store,
                ),
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="save boom"):
        _ = resumed_runtime.resume(
            "approval-session",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )

    replay_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            _load_runtime_types()[1](
                workspace=tmp_path,
                permission_policy=policy,
            ),
        ),
    )
    replay = replay_runtime.resume("approval-session")

    # Reconciliation converts the durable resolved tail into an interrupted
    # checkpoint, so retrying without the approval request id cannot execute the
    # approved tool twice and can finish the graph safely.
    assert replay.session.status == "completed"
    assert replay.events[-1].event_type == "graph.response_ready"
    assert replay.events.count(next(event for event in replay.events if event.event_type == "runtime.approval_resolved")) == 1


def test_runtime_persists_and_resumes_session_across_instances(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("persisted slice\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    first_runtime = runtime_class(workspace=tmp_path)
    first_result = first_runtime.run(runtime_request(prompt="read sample.txt", session_id="demo-session"))

    second_runtime = runtime_class(workspace=tmp_path)
    sessions = second_runtime.list_sessions()
    resumed = second_runtime.resume("demo-session")

    assert [summary.session.id for summary in sessions] == ["demo-session"]
    assert first_result.output == resumed.output
    _assert_ordered_event_types(
        [event.event_type for event in resumed.events],
        [
            "runtime.request_received",
            "runtime.skills_loaded",
            "graph.loop_step",
            "graph.model_turn",
            "graph.tool_request_created",
            "runtime.tool_lookup_succeeded",
            "runtime.permission_resolved",
            "runtime.tool_started",
            "runtime.tool_completed",
            "graph.loop_step",
            "graph.response_ready",
        ],
    )


def _drop_crashed_run_registration(*, workspace: Path, session_id: str) -> None:
    """Forget the abandoned run's process-local registration.

    ``ACTIVE_SESSION_REGISTRY`` records live runs in memory, so a crashed
    process takes its registration with it. These tests abandon a stream inside
    one process instead of killing one, so the phantom registration has to be
    dropped explicitly before the resumed runtime may rewrite the session's
    history (a resume refuses to rewrite history another run still owns).
    """
    active_session_module = importlib.import_module("voidcode.runtime.active_session")
    active_session_module.ACTIVE_SESSION_REGISTRY.unregister(workspace=workspace, session_id=session_id)


def test_runtime_crash_mid_run_marks_interrupted_and_resumes_to_completion(tmp_path: Path) -> None:
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    _ = first_file.write_text("first\n", encoding="utf-8")
    _ = second_file.write_text("second\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="yolo")
    calls = (
        ("read", {"path": str(first_file)}),
        ("read", {"path": str(second_file)}),
    )

    first_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=_SequentialSafeBoundaryGraph(calls), permission_policy=policy)),
    )
    stream = first_runtime.run_stream(runtime_request(prompt="read two files", session_id="crash-session"))
    # The interrupted checkpoint is captured as a side effect at the top of the
    # second loop iteration, so consume past the first tool completion until the
    # second tool request is emitted before abandoning the stream (the crash).
    tool_requests = 0
    for chunk in stream:
        if chunk.event is not None and chunk.event.event_type == "graph.tool_request_created":
            tool_requests += 1
            if tool_requests >= 2:
                break

    _drop_crashed_run_registration(workspace=tmp_path, session_id="crash-session")
    second_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=_SequentialSafeBoundaryGraph(calls), permission_policy=policy)),
    )
    summaries = second_runtime.list_sessions()
    assert [summary.session.id for summary in summaries] == ["crash-session"]
    assert summaries[0].status == "interrupted"

    storage_module = importlib.import_module("voidcode.runtime.storage")
    store = cast(SqliteSessionStore, storage_module.SqliteSessionStore())
    stored = store.load_session(workspace=tmp_path, session_id="crash-session")
    assert stored.session.status == "interrupted"

    resumed = second_runtime.resume("crash-session")

    assert resumed.session.status == "completed"
    assert resumed.output == "done"
    assert [cast(str, event.payload.get("tool")) for event in resumed.events if event.event_type == "runtime.tool_completed"] == [
        "read",
        "read",
    ]


def test_runtime_resume_restores_leaf_and_keeps_orphaned_tail_rows(tmp_path: Path) -> None:
    """An interrupted resume restores the leaf; it never deletes the tail.

    The repair is a position move: the rows a dead run left past its checkpoint
    stay in ``session_events`` (a checkout's abandoned branch lives there too,
    and deleting it would destroy the branch), and only the replay position
    moves back to the checkpoint. The response must then replay the *path*, not
    the flat log, or the retained tail would be handed to the provider as this
    turn's own history.
    """
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    _ = first_file.write_text("first\n", encoding="utf-8")
    _ = second_file.write_text("second\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="yolo")
    calls = (
        ("read", {"path": str(first_file)}),
        ("read", {"path": str(second_file)}),
    )

    first_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=_SequentialSafeBoundaryGraph(calls), permission_policy=policy)),
    )
    stream = first_runtime.run_stream(runtime_request(prompt="read two files", session_id="orphan-session"))
    tool_requests = 0
    for chunk in stream:
        if chunk.event is not None and chunk.event.event_type == "graph.tool_request_created":
            tool_requests += 1
            if tool_requests >= 2:
                break

    _drop_crashed_run_registration(workspace=tmp_path, session_id="orphan-session")
    storage_module = importlib.import_module("voidcode.runtime.storage")
    store = cast(SqliteSessionStore, storage_module.SqliteSessionStore())
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="orphan-session")
    assert checkpoint is not None and checkpoint.get("kind") == "interrupted"
    last_event_sequence = cast(int, checkpoint["last_event_sequence"])

    # Events persisted after the checkpoint but before the crash. Under the tree
    # they are a retained off-path branch, not garbage: the resume restores the
    # leaf behind them and leaves every row in place.
    second_store = cast(SqliteSessionStore, storage_module.SqliteSessionStore())
    second_store.append_session_events(
        workspace=tmp_path,
        session_id="orphan-session",
        events=(
            ("graph.loop_step", "graph", {"step": 999, "phase": "orphan", "orphan": True}, None),
            ("graph.model_turn", "graph", {"turn": 999, "mode": "orphan", "orphan": True}, None),
            ("runtime.tool_completed", "runtime", {"tool": "orphan_tool", "status": "ok", "orphan": True}, None),
        ),
    )

    orphaned_stored = second_store.load_session(workspace=tmp_path, session_id="orphan-session")
    orphaned_markers = [event for event in orphaned_stored.events if event.payload.get("orphan") is True]
    assert len(orphaned_markers) == 3
    assert all(event.sequence > last_event_sequence for event in orphaned_markers)

    resumed_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=_SequentialSafeBoundaryGraph(calls), permission_policy=policy)),
    )
    resumed = resumed_runtime.resume("orphan-session")

    assert resumed.session.status == "completed"
    # The response replays the restored path, so the orphaned tail is absent from
    # the events handed to the caller...
    assert not any(event.payload.get("orphan") is True for event in resumed.events)
    # ...but every row is still in the table, and the run's path simply starts at
    # the checkpoint position instead of the tail.
    final_stored = store.load_session(workspace=tmp_path, session_id="orphan-session")
    surviving_markers = [event for event in final_stored.events if event.payload.get("orphan") is True]
    assert len(surviving_markers) == 3
    path = second_store.session_path(workspace=tmp_path, session_id="orphan-session")
    assert any(event.sequence == last_event_sequence for event in path)
    assert not any(event.payload.get("orphan") is True for event in path)


def test_runtime_multi_tool_call_crash_requeries_provider_from_durable_tool_results(tmp_path: Path) -> None:
    _ = (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    _ = (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    _ = (tmp_path / "c.txt").write_text("c\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="yolo")
    provider_turns_module = importlib.import_module("voidcode.core.provider_turns")
    resolution_module = importlib.import_module("voidcode.provider.resolution")
    registry_module = importlib.import_module("voidcode.provider.registry")
    provider_model = resolution_module.resolve_provider_model(
        "opencode-zen/gpt-5.4",
        registry=registry_module.ModelProviderRegistry.with_defaults(),
    )

    first_provider = _SingleThenBatchTurnProvider()
    first_producer = provider_turns_module.ProviderTurnProducer(provider=first_provider, provider_model=provider_model)
    first_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=first_producer, permission_policy=policy)),
    )
    stream = first_runtime.run_stream(runtime_request(prompt="read three files", session_id="multi-tool-crash"))
    completed_tools = 0
    for chunk in stream:
        if chunk.event is not None and chunk.event.event_type == "runtime.tool_completed":
            completed_tools += 1
            if completed_tools >= 2:
                break

    # Stop after b is durable but before the active batch advances to c. The
    # interrupted checkpoint still records only a's completed result.

    _drop_crashed_run_registration(workspace=tmp_path, session_id="multi-tool-crash")
    storage_module = importlib.import_module("voidcode.runtime.storage")
    store = cast(SqliteSessionStore, storage_module.SqliteSessionStore())
    checkpoint = store.load_resume_checkpoint(workspace=tmp_path, session_id="multi-tool-crash")
    assert checkpoint is not None and checkpoint.get("kind") == "interrupted"
    checkpoint_tool_results = cast(list[object], checkpoint.get("tool_results", []))
    assert [cast(dict[str, object], result).get("tool_name") for result in checkpoint_tool_results] == ["read"]

    resumed_provider = _SingleThenBatchTurnProvider()
    resumed_producer = provider_turns_module.ProviderTurnProducer(provider=resumed_provider, provider_model=provider_model)
    resumed_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, turn_producer=resumed_producer, permission_policy=policy)),
    )
    resumed = resumed_runtime.resume("multi-tool-crash")

    assert resumed.session.status == "completed"
    assert resumed.output == "done"
    completed = [event for event in resumed.events if event.event_type == "runtime.tool_completed"]
    assert [(event.payload.get("tool"), event.payload.get("tool_call_id")) for event in completed] == [
        ("read", "call-a"),
        ("read", "call-b"),
        ("read", "call-c"),
    ]
    # The host restored c from the interrupted batch; the provider was not
    # queried again until all three durable tool results were available.
    assert resumed_provider.propose_turn_tool_result_counts == [3]
