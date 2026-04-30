"""Integration tests for the deterministic read-only slice."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from unittest.mock import ANY, patch

import pytest

pytestmark = pytest.mark.usefixtures("force_deterministic_engine_default")


@pytest.fixture
def force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


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

    def list_background_tasks_by_parent_session(
        self, *, parent_session_id: str
    ) -> tuple[StoredBackgroundTaskSummaryLike, ...]: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskStateLike: ...


class RuntimeFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        tool_registry: object | None = None,
        graph: object | None = None,
        config: object | None = None,
        permission_policy: object | None = None,
        session_store: object | None = None,
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


class AvailableToolLike(Protocol):
    name: str


class ProviderRequestLike(Protocol):
    assembled_context: AssembledContextLike
    available_tools: tuple[AvailableToolLike, ...]


def _assembled_context(request: object) -> AssembledContextLike:
    return cast(ProviderRequestLike, request).assembled_context


def _available_tools(request: object) -> tuple[AvailableToolLike, ...]:
    return cast(ProviderRequestLike, request).available_tools


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


class ReadFileToolType(Protocol):
    invoke: Callable[..., object]


class ToolRegistryLike(Protocol):
    tools: dict[str, object]

    def excluding(self, tool_names: Iterable[str]) -> ToolRegistryLike: ...


class ToolRegistryClassLike(Protocol):
    def with_defaults(self) -> ToolRegistryLike: ...


class SessionStoreLike(Protocol):
    def save_run(
        self,
        *,
        workspace: Path,
        request: RuntimeRequestLike,
        response: RuntimeResponseLike,
        clear_pending_approval: bool = True,
    ) -> None: ...

    def list_sessions(self, *, workspace: Path) -> tuple[StoredSessionSummaryLike, ...]: ...

    def load_session(self, *, workspace: Path, session_id: str) -> RuntimeResponseLike: ...

    def save_pending_approval(
        self,
        *,
        workspace: Path,
        request: RuntimeRequestLike,
        response: RuntimeResponseLike,
        pending_approval: object,
    ) -> None: ...

    def load_pending_approval(self, *, workspace: Path, session_id: str) -> object: ...

    def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None: ...

    def create_background_task(self, *, workspace: Path, task: object) -> None: ...


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)
    runtime_class = cast(RuntimeFactory, service_module.VoidCodeRuntime)
    return runtime_request, runtime_class


@dataclass(frozen=True, slots=True)
class _GraphStep:
    events: tuple[object, ...]
    tool_call: object
    output: str | None = None
    is_finished: bool = False


class _AstGrepPreviewGraph:
    def step(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if not tool_results:
            return _GraphStep(
                events=(),
                tool_call=cast(
                    ToolCallFactory,
                    importlib.import_module("voidcode.tools.contracts").ToolCall,
                )(
                    tool_name="ast_grep_preview",
                    arguments={
                        "pattern": "print($X)",
                        "rewrite": "logger.info($X)",
                        "path": "sample.py",
                        "lang": "python",
                    },
                ),
            )
        return _GraphStep(events=(), tool_call=None, output="previewed", is_finished=True)


class _AstGrepReplaceGraph:
    def step(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if not tool_results:
            return _GraphStep(
                events=(),
                tool_call=cast(
                    ToolCallFactory,
                    importlib.import_module("voidcode.tools.contracts").ToolCall,
                )(
                    tool_name="ast_grep_replace",
                    arguments={
                        "pattern": "print($X)",
                        "rewrite": "logger.info($X)",
                        "path": "sample.py",
                        "lang": "python",
                        "apply": True,
                    },
                ),
            )
        return _GraphStep(events=(), tool_call=None, output="applied", is_finished=True)


class _SingleToolGraph:
    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self._tool_name = tool_name
        self._arguments = arguments

    def step(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if not tool_results:
            return _GraphStep(
                events=(),
                tool_call=cast(
                    ToolCallFactory,
                    importlib.import_module("voidcode.tools.contracts").ToolCall,
                )(
                    tool_name=self._tool_name,
                    arguments=self._arguments,
                ),
            )
        return _GraphStep(events=(), tool_call=None, output="done", is_finished=True)


def _approval_runtime(
    tmp_path: Path, *, mode: str = "ask"
) -> tuple[RuntimeRequestFactory, RuntimeRunner]:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    policy = permission_policy(mode=mode)
    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                permission_policy=policy,
            ),
        ),
    )
    return runtime_request, runtime


def _provider_runtime(
    tmp_path: Path,
    *,
    mode: str = "ask",
) -> tuple[RuntimeRequestFactory, RuntimeRunner]:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    policy = permission_policy(mode=mode)
    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode=mode,
                    execution_engine="provider",
                    model="opencode/gpt-5.4",
                ),
                permission_policy=policy,
            ),
        ),
    )
    return runtime_request, runtime


def test_provider_runtime_surfaces_provider_context_limit_failure_kind(tmp_path: Path) -> None:
    runtime_request, _ = _provider_runtime(tmp_path, mode="allow")

    service_module = importlib.import_module("voidcode.runtime.service")

    class FailingGraph:
        def step(
            self,
            request: object,
            tool_results: tuple[object, ...],
            *,
            session: object,
        ) -> object:
            _ = request, tool_results, session
            raise ValueError("provider context window exceeded")

    failing_runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            graph=FailingGraph(),
            config=importlib.import_module("voidcode.runtime.config").RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
            ),
        ),
    )

    failed = failing_runtime.run(runtime_request(prompt="read sample.txt", session_id="ctx-limit"))

    assert failed.session.status == "failed"
    assert failed.events[-1].event_type == "runtime.failed"
    assert failed.events[-1].payload == {
        "error": "provider context window exceeded",
        "kind": "provider_context_limit",
    }


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
                    return importlib.import_module(
                        "voidcode.runtime.provider_protocol"
                    ).ProviderTurnResult(output="done")
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
                return importlib.import_module(
                    "voidcode.runtime.provider_protocol"
                ).ProviderTurnResult(output="done")

        return _Provider()


@dataclass(frozen=True, slots=True)
class _DelegationE2EModelProvider:
    name: str

    def turn_provider(self) -> object:
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                provider_protocol_module = importlib.import_module(
                    "voidcode.runtime.provider_protocol"
                )
                tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
                assembled_context = _assembled_context(request)
                prompt = assembled_context.prompt
                tool_results = assembled_context.tool_results
                if prompt.startswith("Delegated runtime task."):
                    return provider_protocol_module.ProviderTurnResult(output="child final")
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
                return provider_protocol_module.ProviderTurnResult(
                    output="parent continued after child final"
                )

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
                provider_protocol_module = importlib.import_module(
                    "voidcode.runtime.provider_protocol"
                )
                tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
                assembled_context = _assembled_context(request)
                prompt = assembled_context.prompt
                tool_results = assembled_context.tool_results
                if prompt.startswith("Delegated runtime task."):
                    return provider_protocol_module.ProviderTurnResult(output="child clean")
                if not tool_results:
                    return provider_protocol_module.ProviderTurnResult(
                        tool_call=tool_contracts_module.ToolCall(
                            tool_name="read_file",
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
                provider_protocol_module = importlib.import_module(
                    "voidcode.runtime.provider_protocol"
                )
                tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
                assembled_context = _assembled_context(request)
                prompt = assembled_context.prompt
                tool_results = assembled_context.tool_results
                if prompt.startswith("Delegated runtime task."):
                    return provider_protocol_module.ProviderTurnResult(
                        output="child transcript sentinel"
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
                            tool_name="background_output",
                            arguments={
                                "task_id": task_id,
                                "block": True,
                                "timeout": 3000,
                                "full_session": True,
                                "message_limit": 10,
                            },
                        )
                    )
                return provider_protocol_module.ProviderTurnResult(
                    output="parent collected transcript"
                )

        return _Provider()


def _request_text(request: object) -> str:
    assembled_context = _assembled_context(request)
    segments = assembled_context.segments
    parts: list[str] = [assembled_context.prompt]
    for segment in segments:
        content = segment.content
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


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
        f"background task {task_id} did not reach {sorted(statuses)}; "
        f"last_status={last_task.status if last_task is not None else None!r}"
    )


def _write_demo_skill(skill_dir: Path, *, content: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: Demo skill\n---\n{content}\n",
        encoding="utf-8",
    )


class _ParentBackgroundOutputGraph:
    def step(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request
        session_ref = cast(SessionLike, session).session
        if getattr(session_ref, "parent_id", None) is not None:
            return _GraphStep(
                events=(),
                tool_call=None,
                output="child background final",
                is_finished=True,
            )
        tool_call_factory = cast(
            ToolCallFactory,
            importlib.import_module("voidcode.tools.contracts").ToolCall,
        )
        if not tool_results:
            return _GraphStep(
                events=(),
                tool_call=tool_call_factory(
                    tool_name="task",
                    arguments={
                        "prompt": "finish in the background",
                        "run_in_background": True,
                        "load_skills": [],
                        "subagent_type": "explore",
                        "description": "Background E2E child",
                    },
                ),
            )
        first_result = cast(ToolResultLike, tool_results[0])
        first_data = first_result.data
        if len(tool_results) == 1:
            return _GraphStep(
                events=(),
                tool_call=tool_call_factory(
                    tool_name="background_output",
                    arguments={
                        "task_id": first_data["task_id"],
                        "block": True,
                        "timeout": 3000,
                        "full_session": True,
                    },
                ),
            )
        final_result = cast(ToolResultLike, tool_results[1])
        return _GraphStep(
            events=(),
            tool_call=None,
            output=final_result.content,
            is_finished=True,
        )


class _FailingBackgroundChildGraph:
    def step(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, tool_results
        session_ref = cast(SessionLike, session).session
        if getattr(session_ref, "parent_id", None) is not None:
            raise RuntimeError("delegated child failed twice")
        return _GraphStep(events=(), tool_call=None, output="leader ready", is_finished=True)


class _McpEchoGraph:
    def step(self, request: object, tool_results: tuple[object, ...], *, session: object) -> object:
        _ = request, session
        if not tool_results:
            return _GraphStep(
                events=(),
                tool_call=cast(
                    ToolCallFactory,
                    importlib.import_module("voidcode.tools.contracts").ToolCall,
                )(
                    tool_name="mcp/echo/echo",
                    arguments={"text": "delegated mcp"},
                ),
            )
        return _GraphStep(events=(), tool_call=None, output="mcp parent done", is_finished=True)


def _write_echo_mcp_server(server_script: Path) -> None:
    server_script.write_text(
        r"""
from __future__ import annotations

import json
import sys


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo-mcp", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo text.",
                            "annotations": {"readOnlyHint": True, "destructiveHint": False},
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                            },
                        }
                    ]
                },
            }
        )
        continue
    if method == "tools/call":
        params = message.get("params", {})
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
        text = arguments.get("text", "") if isinstance(arguments, dict) else ""
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": f"echo:{text}"}], "isError": False},
            }
        )
        continue
""",
        encoding="utf-8",
    )


def test_provider_subagent_sync_e2e_parent_task_child_final_and_parent_continuation(
    tmp_path: Path,
) -> None:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(Callable[..., RuntimeRequestLike], contracts_module.RuntimeRequest)

    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
            ),
            permission_policy=permission_module.PermissionPolicy(mode="allow"),
            model_provider_registry=model_provider_module.ModelProviderRegistry(
                providers={"opencode": _DelegationE2EModelProvider(name="opencode")}
            ),
        ),
    )

    response = runtime.run(
        runtime_request(prompt="delegate sync child", session_id="leader-session")
    )
    task_completed = next(
        event
        for event in response.events
        if event.event_type == "runtime.tool_completed" and event.payload["tool"] == "task"
    )
    child_session_id = cast(str, task_completed.payload["session_id"])
    child_replay = runtime.resume(child_session_id)

    assert response.session.status == "completed"
    assert response.output == "parent continued after child final"
    assert task_completed.payload == {
        "tool": "task",
        "tool_call_id": ANY,
        "arguments": {
            "prompt": "return the child final",
            "run_in_background": False,
            "load_skills": [],
            "subagent_type": "explore",
            "description": "Sync subagent E2E child",
        },
        "status": "ok",
        "content": "child final",
        "error": None,
        "session_id": child_session_id,
        "parent_session_id": "leader-session",
        "requested_category": None,
        "requested_subagent_type": "explore",
        "load_skills": [],
        "output": "child final",
        "display": {
            "kind": "task",
            "title": "Task",
            "summary": "Sync subagent E2E child",
            "args": ["explore", "Sync subagent E2E child", "return the child final"],
        },
        "tool_status": {
            "invocation_id": ANY,
            "tool_name": "task",
            "phase": "completed",
            "status": "completed",
            "label": "Sync subagent E2E child",
            "display": {
                "kind": "task",
                "title": "Task",
                "summary": "Sync subagent E2E child",
                "args": ["explore", "Sync subagent E2E child", "return the child final"],
            },
        },
    }
    assert child_replay.session.session.parent_id == "leader-session"
    assert child_replay.output == "child final"
    assert child_replay.session.metadata["delegation"] == {
        "mode": "sync",
        "subagent_type": "explore",
        "description": "Sync subagent E2E child",
        "depth": 1,
        "remaining_spawn_budget": 3,
        "selected_preset": "explore",
        "selected_execution_engine": "provider",
    }


def test_runtime_background_subagent_e2e_collects_background_output_result(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, graph=_ParentBackgroundOutputGraph())),
    )

    response = runtime.run(
        runtime_request(prompt="launch background child", session_id="leader-background")
    )
    task_completed = [
        event
        for event in response.events
        if event.event_type == "runtime.tool_completed" and event.payload["tool"] == "task"
    ][0]
    background_completed = [
        event
        for event in response.events
        if event.event_type == "runtime.tool_completed"
        and event.payload["tool"] == "background_output"
    ][0]
    task_id = cast(str, task_completed.payload["task_id"])
    reloaded = runtime.load_background_task(task_id)

    assert response.session.status == "completed"
    assert response.output is not None
    assert "Background task result digest:" in response.output
    assert "raw child output is not injected" in response.output
    assert task_completed.payload["delegation"] == {
        "mode": "background",
        "subagent_type": "explore",
        "description": "Background E2E child",
        "depth": 1,
        "remaining_spawn_budget": 3,
    }
    assert reloaded.status == "completed"
    assert background_completed.payload["status"] == "ok"
    assert cast(dict[str, object], background_completed.payload["message"])["status"] == "completed"
    assert str(background_completed.payload["summary_output"]).startswith(
        "Completed child session "
    )
    assert background_completed.payload["result_available"] is True
    session_payload = cast(dict[str, object], background_completed.payload["session"])
    assert session_payload["output_available"] is True
    assert session_payload["full_output_preserved"] is True
    assert session_payload["full_session_reference"] == (
        f"session:{session_payload['child_session_id']}"
    )
    assert "output" not in session_payload


def test_runtime_background_subagent_queue_running_completed_states_respect_concurrency(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    config_module = importlib.import_module("voidcode.runtime.config")
    read_file_module = importlib.import_module("voidcode.tools.read_file")
    read_file_tool = cast(ReadFileToolType, read_file_module.ReadFileTool)
    original_invoke = read_file_tool.invoke
    started = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    (tmp_path / "sample.txt").write_text("queued proof\n", encoding="utf-8")

    def _blocking_read(self: object, call: object, *, workspace: Path) -> object:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        started.set()
        _ = release.wait(timeout=2)
        try:
            return original_invoke(self, call, workspace=workspace)
        finally:
            with active_lock:
                active -= 1

    with patch.object(read_file_tool, "invoke", autospec=True, side_effect=_blocking_read):
        runtime = cast(
            RuntimeRunner,
            cast(
                object,
                runtime_class(
                    workspace=tmp_path,
                    config=config_module.RuntimeConfig(
                        execution_engine="deterministic",
                        background_task=config_module.RuntimeBackgroundTaskConfig(
                            default_concurrency=1
                        ),
                    ),
                ),
            ),
        )
        first = runtime.start_background_task(runtime_request(prompt="read sample.txt"))
        assert started.wait(timeout=1) is True
        first_running = runtime.load_background_task(first.task.id)
        second = runtime.start_background_task(runtime_request(prompt="read sample.txt"))
        second_queued = runtime.load_background_task(second.task.id)

        assert first_running.status == "running"
        assert second_queued.status == "queued"
        release.set()
        first_done = _wait_for_background_task_status(
            runtime, first.task.id, {"completed", "failed", "cancelled", "interrupted"}
        )
        second_done = _wait_for_background_task_status(
            runtime, second.task.id, {"completed", "failed", "cancelled", "interrupted"}
        )

    assert first_done.status == "completed"
    assert second_done.status == "completed"
    assert max_active == 1


def test_runtime_background_subagent_failure_guides_session_id_retry_and_escalation(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
    runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, graph=_FailingBackgroundChildGraph())),
    )
    _ = runtime.run(runtime_request(prompt="leader", session_id="leader-failure"))
    started = runtime.start_background_task(
        runtime_request(
            prompt="fail child",
            session_id="retry-child",
            parent_session_id="leader-failure",
            metadata={"delegation": {"mode": "background", "subagent_type": "explore"}},
        )
    )
    failed = _wait_for_background_task_status(runtime, started.task.id, {"failed"})
    background_output_tool = importlib.import_module(
        "voidcode.tools.background_output"
    ).BackgroundOutputTool(runtime=runtime)

    result = background_output_tool.invoke(
        tool_contracts_module.ToolCall(
            tool_name="background_output",
            arguments={"task_id": started.task.id, "full_session": True},
        ),
        workspace=tmp_path,
    )

    assert failed.status == "failed"
    assert result.status == "ok"
    assert result.data["status"] == "failed"
    assert result.data["child_session_id"] == "retry-child"
    assert "session_id='retry-child'" in cast(str, result.data["guidance"])
    assert "After repeated failures, stop retrying and escalate" in cast(
        str, result.data["guidance"]
    )


def test_runtime_delegated_skill_loaded_child_records_exact_skill_metadata(
    tmp_path: Path,
) -> None:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    provider_protocol_module = importlib.import_module("voidcode.runtime.provider_protocol")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(Callable[..., RuntimeRequestLike], contracts_module.RuntimeRequest)
    requests: list[object] = []
    _write_demo_skill(
        tmp_path / ".voidcode" / "skills" / "demo",
        content="# Demo\nUse the delegated integration skill body.",
    )

    _ = provider_protocol_module
    # Capture the provider request with a small wrapper so the test asserts prompt injection.
    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
                skills=config_module.RuntimeSkillsConfig(enabled=True),
            ),
            permission_policy=permission_module.PermissionPolicy(mode="allow"),
            model_provider_registry=model_provider_module.ModelProviderRegistry(
                providers={"opencode": _CapturingModelProvider(name="opencode", requests=requests)}
            ),
        ),
    )

    response = runtime.run(
        runtime_request(
            prompt="skill child",
            session_id="delegated-skill-e2e",
            metadata={
                "force_load_skills": ["demo"],
                "delegation": {"mode": "sync", "subagent_type": "explore"},
            },
        )
    )
    skills_applied = next(
        event for event in response.events if event.event_type == "runtime.skills_applied"
    )
    system_contents = [
        segment.content
        for segment in _assembled_context(requests[0]).segments
        if segment.role == "system"
    ]

    assert response.session.status == "completed"
    assert response.session.metadata["selected_skill_names"] == ["demo"]
    assert response.session.metadata["applied_skills"] == ["demo"]
    assert skills_applied.payload["skills"] == ["demo"]
    assert skills_applied.payload["count"] == 1
    assert skills_applied.payload["prompt_context_built"] is True
    assert isinstance(skills_applied.payload["prompt_context_length"], int)
    assert any(
        isinstance(content, str) and "Use the delegated integration skill body." in content
        for content in system_contents
    )


def test_provider_runtime_persists_and_injects_runtime_todo_state(tmp_path: Path) -> None:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    provider_protocol_module = importlib.import_module("voidcode.runtime.provider_protocol")
    service_module = importlib.import_module("voidcode.runtime.service")
    tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
    runtime_request = cast(Callable[..., RuntimeRequestLike], contracts_module.RuntimeRequest)
    requests: list[object] = []

    class _TodoModelProvider:
        def turn_provider(self) -> object:
            class _Provider:
                name = "opencode"

                def propose_turn(self, request: object) -> object:
                    requests.append(request)
                    if len(requests) == 1:
                        return provider_protocol_module.ProviderTurnResult(
                            tool_call=tool_contracts_module.ToolCall(
                                tool_name="todo_write",
                                arguments={
                                    "todos": [
                                        {
                                            "content": "make todo runtime-owned",
                                            "status": "in_progress",
                                            "priority": "high",
                                        },
                                        {
                                            "content": "document completed setup",
                                            "status": "completed",
                                            "priority": "low",
                                        },
                                    ]
                                },
                            )
                        )
                    if len(requests) == 2:
                        return provider_protocol_module.ProviderTurnResult(
                            tool_call=tool_contracts_module.ToolCall(
                                tool_name="todo_write",
                                arguments={
                                    "todos": [
                                        {
                                            "content": "verify latest todo context only",
                                            "status": "pending",
                                            "priority": "medium",
                                        }
                                    ]
                                },
                            )
                        )
                    return provider_protocol_module.ProviderTurnResult(output="done")

            return _Provider()

    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
                context_window=config_module.RuntimeContextWindowConfig(max_tool_results=1),
            ),
            permission_policy=permission_module.PermissionPolicy(mode="allow"),
            model_provider_registry=model_provider_module.ModelProviderRegistry(
                providers={"opencode": _TodoModelProvider()}
            ),
        ),
    )

    response = runtime.run(
        runtime_request(prompt="track todo state", session_id="runtime-todo-session")
    )
    loaded = runtime.session_result(session_id="runtime-todo-session")
    todo_events = tuple(
        event for event in response.events if event.event_type == "runtime.todo_updated"
    )
    second_request_system_segments = [
        segment.content
        for segment in _assembled_context(requests[1]).segments
        if segment.role == "system"
    ]
    third_request_system_segments = [
        segment.content
        for segment in _assembled_context(requests[2]).segments
        if segment.role == "system"
    ]

    assert response.session.status == "completed"
    assert len(todo_events) == 2
    todo_event = todo_events[0]
    latest_todo_event = todo_events[1]
    assert todo_event.payload["active_count"] == 1
    assert todo_event.payload["pending_count"] == 0
    assert todo_event.payload["in_progress_count"] == 1
    assert todo_event.payload["completed_count"] == 1
    assert latest_todo_event.payload["pending_count"] == 1
    assert any(
        isinstance(content, str)
        and "Runtime-managed todo state is active" in content
        and "make todo runtime-owned" in content
        and "document completed setup" not in content
        for content in second_request_system_segments
    )
    latest_todo_segments = [
        content
        for content in third_request_system_segments
        if isinstance(content, str) and content.startswith("Runtime-managed todo state is active")
    ]
    assert len(latest_todo_segments) == 1
    assert "verify latest todo context only" in latest_todo_segments[0]
    assert "make todo runtime-owned" not in latest_todo_segments[0]
    raw_runtime_state = loaded.session.metadata["runtime_state"]
    assert isinstance(raw_runtime_state, dict)
    assert "todos" in raw_runtime_state


def test_runtime_delegated_mcp_and_background_hook_events_have_exact_metadata(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    config_module = importlib.import_module("voidcode.runtime.config")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    server_script = tmp_path / "echo_mcp_server.py"
    _write_echo_mcp_server(server_script)

    mcp_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=_McpEchoGraph(),
                permission_policy=permission_module.PermissionPolicy(mode="allow"),
                config=config_module.RuntimeConfig(
                    approval_mode="allow",
                    execution_engine="deterministic",
                    mcp=config_module.RuntimeMcpConfig(
                        enabled=True,
                        servers={
                            "echo": config_module.RuntimeMcpServerConfig(
                                transport="stdio",
                                command=(sys.executable, str(server_script)),
                                scope="session",
                            )
                        },
                    ),
                    agents={
                        "explore": config_module.RuntimeAgentConfig(
                            preset="explore",
                            execution_engine="deterministic",
                        )
                    },
                ),
            ),
        ),
    )

    response = mcp_runtime.run(runtime_request(prompt="leader", session_id="leader-mcp"))
    assert response.session.status == "completed"
    response = mcp_runtime.run(
        runtime_request(
            prompt="use mcp",
            session_id="mcp-child",
            parent_session_id="leader-mcp",
        )
    )
    event_types = [event.event_type for event in response.events]
    mcp_started = next(
        event for event in response.events if event.event_type == "runtime.mcp_server_started"
    )
    mcp_released = next(
        event for event in response.events if event.event_type == "runtime.mcp_server_released"
    )
    mcp_tool_completed = next(
        event
        for event in response.events
        if event.event_type == "runtime.tool_completed" and event.payload["server"] == "echo"
    )

    assert response.session.status == "completed"
    assert "runtime.mcp_server_stopped" in event_types
    assert mcp_started.payload["server"] == "echo"
    assert mcp_started.payload["scope"] == "session"
    assert mcp_started.payload["owner_session_id"] == "mcp-child"
    assert mcp_released.payload["owner_session_id"] == "mcp-child"
    assert mcp_tool_completed.payload["tool"] == "echo"
    assert mcp_tool_completed.payload["content"] == "echo:delegated mcp"

    hook_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=_ParentBackgroundOutputGraph(),
                config=config_module.RuntimeConfig(
                    approval_mode="allow",
                    execution_engine="deterministic",
                    hooks=config_module.RuntimeHooksConfig(
                        enabled=True,
                        on_background_task_completed=((sys.executable, "-c", "print('hook ok')"),),
                    ),
                ),
                permission_policy=permission_module.PermissionPolicy(mode="allow"),
            ),
        ),
    )
    hook_response = hook_runtime.run(
        runtime_request(prompt="launch hooked background", session_id="leader-hooked-background")
    )
    background_event = next(
        event
        for event in hook_runtime.resume("leader-hooked-background").events
        if event.event_type == "runtime.background_task_completed"
    )
    delegation = cast(dict[str, object], background_event.payload["delegation"])
    message = cast(dict[str, object], background_event.payload["message"])

    assert hook_response.session.status == "completed"
    assert delegation["parent_session_id"] == "leader-hooked-background"
    assert delegation["child_session_id"] == background_event.payload["child_session_id"]
    assert delegation["routing"] == {
        "mode": "background",
        "subagent_type": "explore",
        "description": "Background E2E child",
    }
    assert delegation["selected_preset"] == "explore"
    assert delegation["selected_execution_engine"] == "provider"
    assert delegation["lifecycle_status"] == "completed"
    assert str(message["summary_output"]).startswith("Completed child session ")
    assert "child background final" not in str(message["summary_output"])
    assert message == {
        "kind": "delegated_lifecycle",
        "status": "completed",
        "summary_output": message["summary_output"],
        "error": None,
        "approval_blocked": False,
        "result_available": True,
    }


def test_runtime_background_restart_reconcile_reloads_terminal_delegated_result(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    first_runtime = cast(
        RuntimeRunner,
        cast(object, runtime_class(workspace=tmp_path, graph=_ParentBackgroundOutputGraph())),
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
        cast(object, runtime_class(workspace=tmp_path, graph=_ParentBackgroundOutputGraph())),
    )
    reloaded = second_runtime.load_background_task(started.task.id)
    task_result = cast(Any, second_runtime).load_background_task_result(started.task.id)

    assert completed.status == "completed"
    assert reloaded.status == "completed"
    assert task_result.status == "completed"
    assert str(task_result.summary_output).startswith("Completed child session ")
    assert "child background final" not in str(task_result.summary_output)
    assert task_result.result_available is True


def test_provider_child_request_excludes_parent_tool_results_and_transcript_by_default(
    tmp_path: Path,
) -> None:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(Callable[..., RuntimeRequestLike], contracts_module.RuntimeRequest)
    requests: list[object] = []
    _ = (tmp_path / "parent-secret.txt").write_text("parent-only tool result\n", encoding="utf-8")

    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
            ),
            permission_policy=permission_module.PermissionPolicy(mode="allow"),
            model_provider_registry=model_provider_module.ModelProviderRegistry(
                providers={
                    "opencode": _ParentToolResultGuardrailProvider(
                        name="opencode",
                        requests=requests,
                    )
                }
            ),
        ),
    )

    response = runtime.run(
        runtime_request(prompt="parent reads then delegates", session_id="leader-context")
    )
    child_request = next(
        request
        for request in requests
        if _assembled_context(request).prompt.startswith("Delegated runtime task.")
    )
    parent_followup_request = next(
        request for request in requests if len(_assembled_context(request).tool_results) == 1
    )
    child_context = _assembled_context(child_request)

    assert response.session.status == "completed"
    assert response.output == "parent done"
    assert "parent-only tool result" in _request_text(parent_followup_request)
    assert child_context.tool_results == ()
    assert "parent-only tool result" not in _request_text(child_request)
    assert "runtime.request_received" not in _request_text(child_request)
    assert "runtime.tool_completed" not in _request_text(child_request)
    assert "transcript" not in child_context.metadata


def test_provider_background_output_full_session_is_tool_result_not_hidden_context(
    tmp_path: Path,
) -> None:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(Callable[..., RuntimeRequestLike], contracts_module.RuntimeRequest)
    requests: list[object] = []

    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
            ),
            permission_policy=permission_module.PermissionPolicy(mode="allow"),
            model_provider_registry=model_provider_module.ModelProviderRegistry(
                providers={
                    "opencode": _BackgroundOutputGuardrailProvider(
                        name="opencode",
                        requests=requests,
                    )
                }
            ),
        ),
    )

    response = runtime.run(
        runtime_request(prompt="launch and collect background", session_id="leader-bg")
    )
    parent_requests = [
        request
        for request in requests
        if not _assembled_context(request).prompt.startswith("Delegated runtime task.")
    ]
    after_task_request = next(
        request for request in parent_requests if len(_assembled_context(request).tool_results) == 1
    )
    after_background_output_request = next(
        request for request in parent_requests if len(_assembled_context(request).tool_results) == 2
    )
    after_background_context = _assembled_context(after_background_output_request)
    background_output_result = after_background_context.tool_results[1]
    background_output_data = background_output_result.data
    background_output_session = cast(dict[str, object], background_output_data["session"])
    background_tool_segments = [
        segment
        for segment in after_background_context.segments
        if segment.role == "tool" and segment.tool_name == "background_output"
    ]

    assert response.session.status == "completed"
    assert response.output == "parent collected transcript"
    assert "child transcript sentinel" not in _request_text(after_task_request)
    assert background_output_result.tool_name == "background_output"
    assert background_output_session["output_available"] is True
    assert background_output_session["full_output_preserved"] is True
    assert "output" not in background_output_session
    assert isinstance(background_output_session["transcript_count"], int)
    assert background_output_session["transcript_count"] > 0
    assert background_tool_segments
    assert isinstance(background_tool_segments[0].content, str)
    assert "Background task result digest:" in background_tool_segments[0].content
    assert "child transcript sentinel" not in background_tool_segments[0].content
    assert (
        getattr(background_output_result, "reference", None)
        == background_output_session["full_session_reference"]
    )
    assert all(
        "child transcript sentinel" not in segment.content
        for segment in after_background_context.segments
        if segment.role == "system" and isinstance(segment.content, str)
    )


def test_provider_visible_tools_are_filtered_for_delegated_agent_presets(
    tmp_path: Path,
) -> None:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(Callable[..., RuntimeRequestLike], contracts_module.RuntimeRequest)

    cases = (
        (
            "explore",
            {"write_file", "edit", "multi_edit", "apply_patch", "task"},
            {"read_file", "grep", "glob"},
        ),
        ("worker", {"task"}, {"read_file", "write_file", "edit", "apply_patch"}),
    )
    for subagent_type, denied_tools, expected_tools in cases:
        requests: list[object] = []
        runtime = cast(
            RuntimeRunner,
            service_module.VoidCodeRuntime(
                workspace=tmp_path,
                config=config_module.RuntimeConfig(
                    approval_mode="allow",
                    execution_engine="provider",
                    model="opencode/gpt-5.4",
                ),
                permission_policy=permission_module.PermissionPolicy(mode="allow"),
                model_provider_registry=model_provider_module.ModelProviderRegistry(
                    providers={
                        "opencode": _CapturingModelProvider(
                            name="opencode",
                            requests=requests,
                        )
                    }
                ),
            ),
        )

        response = runtime.run(
            runtime_request(
                prompt=f"inspect {subagent_type} tools",
                session_id=f"{subagent_type}-visible-tools",
                metadata={
                    "delegation": {
                        "mode": "sync",
                        "subagent_type": subagent_type,
                        "description": f"inspect {subagent_type} tools",
                    }
                },
            )
        )

        assert response.session.status == "completed"
        assert requests
        available_tools = _available_tools(requests[0])
        visible_tool_names = {tool.name for tool in available_tools}
        assert expected_tools.issubset(visible_tool_names)
        assert denied_tools.isdisjoint(visible_tool_names)


@pytest.mark.parametrize(
    ("subagent_type", "tool_name", "arguments"),
    [
        ("explore", "write_file", {"path": "blocked.txt", "content": "blocked"}),
        ("advisor", "apply_patch", {"patch": "*** Begin Patch\n*** End Patch"}),
        (
            "worker",
            "task",
            {
                "description": "nested delegation",
                "prompt": "read sample.txt",
                "subagent_type": "explore",
            },
        ),
    ],
)
def test_runtime_rejects_denied_raw_provider_tool_calls_for_delegated_agents(
    tmp_path: Path,
    subagent_type: str,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    provider_protocol_module = importlib.import_module("voidcode.runtime.provider_protocol")
    service_module = importlib.import_module("voidcode.runtime.service")
    tool_contracts_module = importlib.import_module("voidcode.tools.contracts")
    runtime_request = cast(Callable[..., RuntimeRequestLike], contracts_module.RuntimeRequest)

    target = tmp_path / "blocked.txt"
    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
            ),
            permission_policy=permission_module.PermissionPolicy(mode="allow"),
            model_provider_registry=model_provider_module.ModelProviderRegistry(
                providers={
                    "opencode": _ScriptedModelProvider(
                        name="opencode",
                        outcomes=(
                            provider_protocol_module.ProviderTurnResult(
                                tool_call=tool_contracts_module.ToolCall(
                                    tool_name=tool_name,
                                    arguments=arguments,
                                )
                            ),
                        ),
                    )
                }
            ),
        ),
    )
    events: list[EventLike] = []

    with pytest.raises(ValueError, match="delegation policy denied tool"):
        for chunk in runtime.run_stream(
            runtime_request(
                prompt=f"malicious {subagent_type} calls {tool_name}",
                session_id=f"{subagent_type}-{tool_name}-denied",
                metadata={
                    "delegation": {
                        "mode": "sync",
                        "subagent_type": subagent_type,
                        "description": f"malicious {subagent_type} calls {tool_name}",
                    }
                },
            )
        ):
            if chunk.event is not None:
                events.append(chunk.event)

    assert events[-1].event_type == "runtime.failed"
    assert events[-1].payload == {
        "error": (
            f"delegation policy denied tool '{tool_name}' for child preset '{subagent_type}'; "
            "this preset may only call tools allowed by its manifest tool_allowlist"
        ),
        "kind": "delegation_tool_policy_denied",
        "tool": tool_name,
    }
    assert target.exists() is False


def test_provider_runtime_falls_back_to_next_provider_target(tmp_path: Path) -> None:
    runtime_request, _ = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    provider_protocol_module = importlib.import_module("voidcode.runtime.provider_protocol")
    service_module = importlib.import_module("voidcode.runtime.service")

    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="provider",
                model="opencode/gpt-5.4",
                provider_fallback=config_module.RuntimeProviderFallbackConfig(
                    preferred_model="opencode/gpt-5.4",
                    fallback_models=("custom/demo",),
                ),
            ),
            permission_policy=permission_module.PermissionPolicy(mode="allow"),
            model_provider_registry=model_provider_module.ModelProviderRegistry(
                providers={
                    "opencode": _ScriptedModelProvider(
                        name="opencode",
                        outcomes=(
                            provider_protocol_module.ProviderExecutionError(
                                kind="rate_limit",
                                provider_name="opencode",
                                model_name="gpt-5.4",
                                message="too many requests",
                            ),
                        ),
                    ),
                    "custom": _ScriptedModelProvider(
                        name="custom",
                        outcomes=(
                            provider_protocol_module.ProviderTurnResult(output="fallback ok"),
                        ),
                    ),
                }
            ),
        ),
    )

    response = runtime.run(runtime_request(prompt="read sample.txt", session_id="fallback-run"))

    assert response.session.status == "completed"
    assert response.output == "fallback ok"
    assert [event.event_type for event in response.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "runtime.provider_fallback",
        "graph.loop_step",
        "graph.model_turn",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert response.events[2].payload == {
        "reason": "rate_limit",
        "from_provider": "opencode",
        "from_model": "gpt-5.4",
        "to_provider": "custom",
        "to_model": "demo",
        "attempt": 1,
    }


def _multi_step_prompt() -> str:
    return "read source.txt\nwrite copied.txt copied marker\ngrep copied copied.txt"


def _cli_test_env() -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[2] / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    )
    return env


def _normalize_terminal_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _run_cli_in_tty(
    *,
    workspace: Path,
    request: str,
    session_id: str,
    approval_input: str,
) -> subprocess.CompletedProcess[str]:
    script = shutil.which("script")
    if script is None:
        pytest.skip("requires script for TTY-backed CLI integration")
    probe = subprocess.run(
        [script, "-qefc", "printf ''", "/dev/null"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("requires script with -qefc support for TTY-backed CLI integration")

    command = shlex.join(
        [
            sys.executable,
            "-m",
            "voidcode",
            "run",
            request,
            "--workspace",
            str(workspace),
            "--session-id",
            session_id,
            "--approval-mode",
            "ask",
        ]
    )
    return subprocess.run(
        ["script", "-qefc", command, "/dev/null"],
        input=f"{approval_input}\n",
        capture_output=True,
        text=True,
        check=False,
        env=_cli_test_env(),
    )


def test_runtime_allows_non_read_only_tool_when_policy_is_allow(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")

    allowed = runtime.run(
        runtime_request(prompt="write danger.txt approved write", session_id="allow-session")
    )

    assert allowed.session.status == "completed"
    assert [event.event_type for event in allowed.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert allowed.events[6].payload["decision"] == "allow"
    assert allowed.output == "Wrote file successfully: danger.txt"
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved write"


def test_runtime_tool_request_created_supports_non_path_tool_arguments(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")

    command = _cwd_command()
    prompt = f"run {command}"
    result = runtime.run(runtime_request(prompt=prompt, session_id="command-session"))

    assert result.events[1].event_type == "runtime.skills_loaded"
    assert result.events[1].payload["skills"] == []
    assert result.events[1].payload["selected_skills"] == []
    assert result.events[1].payload["catalog_context_length"] == 0
    assert result.events[2].event_type == "graph.loop_step"
    assert result.events[3].event_type == "graph.model_turn"
    tool_request_event = result.events[4]
    command = _cwd_command()
    assert tool_request_event.event_type == "graph.tool_request_created"
    assert tool_request_event.payload == {
        "tool": "shell_exec",
        "arguments": {"command": command},
    }


def test_runtime_allows_shell_exec_tool_when_policy_is_allow(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")

    command = _cwd_command()
    prompt = f"run {command}"
    allowed = runtime.run(runtime_request(prompt=prompt, session_id="shell-allow-session"))

    assert allowed.session.status == "completed"
    assert [event.event_type for event in allowed.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert allowed.events[6].payload["decision"] == "allow"
    assert allowed.output == f"{tmp_path.resolve()}\n"
    assert allowed.events[8].payload["command"] == command
    assert allowed.events[8].payload["exit_code"] == 0


def test_runtime_requests_and_resumes_shell_exec_approval(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    command = _cwd_command()
    prompt = f"run {command}"
    waiting = runtime.run(runtime_request(prompt=prompt, session_id="shell-approval-session"))

    assert waiting.session.status == "waiting"
    assert [event.event_type for event in waiting.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]
    assert waiting.events[-1].payload["tool"] == "shell_exec"
    assert waiting.events[-1].payload["arguments"] == {"command": command}
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    resumed = runtime.resume(
        "shell-approval-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert [event.event_type for event in resumed.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert resumed.output == f"{tmp_path.resolve()}\n"
    assert resumed.events[9].payload["command"] == command
    assert resumed.events[9].payload["exit_code"] == 0


def test_runtime_denies_shell_exec_tool_when_policy_is_deny(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="deny")

    command = _cwd_command()
    prompt = f"run {command}"
    denied = runtime.run(runtime_request(prompt=prompt, session_id="shell-deny-session"))

    assert denied.session.status == "failed"
    assert [event.event_type for event in denied.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert denied.events[6].payload["decision"] == "deny"
    assert denied.output is None


def test_runtime_emits_pre_and_post_hook_events_around_successful_tool_run(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    hooks_config = cast(Callable[..., object], config_module.RuntimeHooksConfig)
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                permission_policy=policy,
                config=runtime_config(
                    approval_mode="allow",
                    execution_engine="deterministic",
                    hooks=hooks_config(
                        enabled=True,
                        pre_tool=((sys.executable, "-c", "print('pre ok')"),),
                        post_tool=((sys.executable, "-c", "print('post ok')"),),
                    ),
                ),
            ),
        ),
    )

    command = _cwd_command()
    prompt = f"run {command}"
    result = runtime.run(runtime_request(prompt=prompt, session_id="hook-success-session"))

    assert [event.event_type for event in result.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_hook_pre",
        "runtime.tool_started",
        "runtime.tool_completed",
        "runtime.tool_hook_post",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert result.events[7].payload == {
        "phase": "pre",
        "tool_name": "shell_exec",
        "session_id": "hook-success-session",
        "status": "ok",
    }
    assert result.events[10].payload == {
        "phase": "post",
        "tool_name": "shell_exec",
        "session_id": "hook-success-session",
        "status": "ok",
    }


def test_runtime_aborts_tool_run_when_pre_hook_fails(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    hooks_config = cast(Callable[..., object], config_module.RuntimeHooksConfig)
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")

    shell_exec_module = importlib.import_module("voidcode.tools.shell_exec")
    shell_exec_tool = cast(ReadFileToolType, shell_exec_module.ShellExecTool)

    with patch.object(shell_exec_tool, "invoke", autospec=True) as invoke_mock:
        runtime = cast(
            RuntimeRunner,
            cast(
                object,
                runtime_class(
                    workspace=tmp_path,
                    permission_policy=policy,
                    config=runtime_config(
                        approval_mode="allow",
                        execution_engine="deterministic",
                        hooks=hooks_config(
                            enabled=True,
                            pre_tool=((sys.executable, "-c", "import sys; sys.exit(7)"),),
                        ),
                    ),
                ),
            ),
        )

        with pytest.raises(RuntimeError, match="hook"):
            command = _cwd_command()
            prompt = f"run {command}"
            _ = runtime.run(runtime_request(prompt=prompt, session_id="hook-pre-fail-session"))

    invoke_mock.assert_not_called()

    replay_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                permission_policy=policy,
                config=runtime_config(
                    approval_mode="allow",
                    execution_engine="deterministic",
                    hooks=hooks_config(
                        enabled=True,
                        pre_tool=((sys.executable, "-c", "import sys; sys.exit(7)"),),
                    ),
                ),
            ),
        ),
    )
    replay = replay_runtime.resume("hook-pre-fail-session")

    assert replay.events[-2].event_type == "runtime.tool_hook_pre"
    assert replay.events[-2].payload["status"] == "error"
    assert replay.events[-1].event_type == "runtime.failed"
    assert replay.session.status == "failed"


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
                "approval_mode": "allow",
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


def test_runtime_skips_post_hook_when_tool_execution_fails(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    hooks_config = cast(Callable[..., object], config_module.RuntimeHooksConfig)
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")

    write_file_module = importlib.import_module("voidcode.tools.write_file")
    write_tool = cast(ReadFileToolType, write_file_module.WriteFileTool)

    def _failing_write_invoke(_self: object, _call: object, *, workspace: Path) -> object:
        _ = workspace
        raise RuntimeError("tool boom")

    with patch.object(write_tool, "invoke", autospec=True, side_effect=_failing_write_invoke):
        runtime = cast(
            RuntimeRunner,
            cast(
                object,
                runtime_class(
                    workspace=tmp_path,
                    permission_policy=policy,
                    config=runtime_config(
                        approval_mode="allow",
                        execution_engine="deterministic",
                        hooks=hooks_config(
                            enabled=True,
                            pre_tool=((sys.executable, "-c", "print('pre ok')"),),
                            post_tool=((sys.executable, "-c", "print('post ok')"),),
                        ),
                    ),
                ),
            ),
        )

        with pytest.raises(RuntimeError, match="tool boom"):
            runtime.run(
                runtime_request(prompt="write danger.txt should fail", session_id="hook-tool-fail")
            )

    replay_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                permission_policy=policy,
                config=runtime_config(
                    approval_mode="allow",
                    execution_engine="deterministic",
                    hooks=hooks_config(
                        enabled=True,
                        pre_tool=((sys.executable, "-c", "print('pre ok')"),),
                        post_tool=((sys.executable, "-c", "print('post ok')"),),
                    ),
                ),
            ),
        ),
    )
    replay = replay_runtime.resume("hook-tool-fail")

    assert replay.session.status == "failed"
    failed = next(event for event in replay.events if event.event_type == "runtime.failed")
    assert failed.payload["error"] == "tool boom"
    assert all(event.event_type != "runtime.tool_hook_post" for event in replay.events)


def test_runtime_persists_initial_allow_tool_failure_for_resume(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime = runtime_class(workspace=tmp_path, permission_policy=policy)
    write_file_module = importlib.import_module("voidcode.tools.write_file")

    write_tool = cast(ReadFileToolType, write_file_module.WriteFileTool)

    def _failing_write_invoke(_self: object, _call: object, *, workspace: Path) -> object:
        _ = workspace
        raise RuntimeError("boom")

    with patch.object(write_tool, "invoke", autospec=True, side_effect=_failing_write_invoke):
        with pytest.raises(RuntimeError, match="boom"):
            runtime.run(runtime_request(prompt="write danger.txt broken", session_id="s1"))

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
    resumed = replay_runtime.resume("s1")

    assert resumed.session.status == "failed"
    failed = next(event for event in resumed.events if event.event_type == "runtime.failed")
    assert failed.payload["error"] == "boom"


def test_runtime_persists_initial_allow_finalize_failure_for_resume(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")

    class FailingFinalizeGraph:
        def step(
            self, request: object, tool_results: tuple[object, ...], *, session: object
        ) -> object:
            if not tool_results:
                return _GraphStep(
                    events=(),
                    tool_call=cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name="write_file",
                        arguments={"path": "danger.txt", "content": "broken finalize"},
                    ),
                )
            raise RuntimeError("finalize boom")

    failing_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=FailingFinalizeGraph(),
                permission_policy=policy,
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="finalize boom"):
        _ = failing_runtime.run(
            runtime_request(prompt="write danger.txt broken finalize", session_id="s1")
        )

    replay_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=FailingFinalizeGraph(),
                permission_policy=policy,
            ),
        ),
    )
    resumed = replay_runtime.resume("s1")

    assert resumed.session.status == "failed"
    assert resumed.events[-2].event_type == "runtime.tool_completed"
    assert resumed.events[-1].event_type == "runtime.failed"
    assert resumed.events[-1].payload == {"error": "finalize boom"}


def test_runtime_persists_initial_plan_failure_for_resume(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")

    class FailingPlanGraph:
        def step(
            self, request: object, tool_results: tuple[object, ...], *, session: object
        ) -> object:
            if not tool_results:
                raise RuntimeError("plan boom")
            raise AssertionError("finalize should not run")

    failing_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=FailingPlanGraph(),
                permission_policy=policy,
            ),
        ),
    )

    with pytest.raises(RuntimeError, match="plan boom"):
        _ = failing_runtime.run(
            runtime_request(prompt="write danger.txt anything", session_id="s1")
        )

    replay_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=FailingPlanGraph(),
                permission_policy=policy,
            ),
        ),
    )
    resumed = replay_runtime.resume("s1")

    assert resumed.session.status == "failed"
    assert resumed.events[-1].event_type == "runtime.failed"
    assert resumed.events[-1].payload == {"error": "plan boom"}


def test_runtime_denies_non_read_only_tool_when_policy_is_deny(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="deny")

    denied = runtime.run(
        runtime_request(prompt="write danger.txt denied write", session_id="deny-session")
    )

    assert denied.session.status == "failed"
    assert [event.event_type for event in denied.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert denied.events[6].payload["decision"] == "deny"
    assert denied.output is None
    assert (tmp_path / "danger.txt").exists() is False


def test_runtime_allows_ast_grep_preview_when_policy_is_deny(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="deny")
    sample_file = tmp_path / "sample.py"
    _ = sample_file.write_text("print('hello')\n", encoding="utf-8")

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=_AstGrepPreviewGraph(),
                permission_policy=policy,
            ),
        ),
    )
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            '{"text":"print(\'hello\')","file":"sample.py","replacement":"logger.info(\'hello\')"}\n'
        ),
        stderr="",
    )

    with patch("subprocess.run", return_value=completed):
        result = runtime.run(runtime_request(prompt="preview", session_id="ast-grep-preview-deny"))

    assert result.session.status == "completed"
    event_types = [event.event_type for event in result.events]
    assert "runtime.permission_resolved" in event_types
    assert "runtime.approval_requested" not in event_types
    assert result.output == "previewed"


def test_runtime_requests_approval_for_ast_grep_replace_when_policy_is_ask(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")
    sample_file = tmp_path / "sample.py"
    _ = sample_file.write_text("print('hello')\n", encoding="utf-8")

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=_AstGrepReplaceGraph(),
                permission_policy=policy,
            ),
        ),
    )

    waiting = runtime.run(runtime_request(prompt="replace", session_id="ast-grep-replace-ask"))

    assert waiting.session.status == "waiting"
    event_types = [event.event_type for event in waiting.events]
    assert event_types[-1] == "runtime.approval_requested"
    assert "runtime.tool_lookup_succeeded" in event_types
    assert waiting.events[-1].payload["tool"] == "ast_grep_replace"
    assert waiting.events[-1].payload["arguments"] == {
        "pattern": "print($X)",
        "rewrite": "logger.info($X)",
        "path": "sample.py",
        "lang": "python",
        "apply": True,
    }


def test_runtime_does_not_request_external_approval_for_ast_grep_external_path(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")

    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_config = cast(
        Callable[..., object],
        config_module.ExternalDirectoryPermissionConfig,
    )
    policy_config = cast(Callable[..., object], config_module.ExternalDirectoryPolicy)

    outside_file = tmp_path.parent / "external-ast-grep.py"
    outside_file.write_text("print('outside')\n", encoding="utf-8")
    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode="allow",
                    permission=permission_config(
                        read=policy_config(rules=(("*", "ask"),)),
                    ),
                ),
                graph=_SingleToolGraph(
                    "ast_grep_search",
                    {"pattern": "print($X)", "path": str(outside_file), "lang": "python"},
                ),
                permission_policy=policy,
            ),
        ),
    )

    result = runtime.run(
        runtime_request(prompt="external ast grep", session_id="ast-grep-external")
    )
    event_types = [event.event_type for event in result.events]
    completed_event = next(
        event for event in result.events if event.event_type == "runtime.tool_completed"
    )

    assert result.session.status == "completed"
    assert "runtime.approval_requested" not in event_types
    assert completed_event.payload["tool"] == "ast_grep_search"
    assert completed_event.payload["status"] == "error"
    assert "inside the workspace" in str(completed_event.payload["error"])


def test_runtime_requests_external_read_approval_with_context_payload(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")

    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_config = cast(
        Callable[..., object],
        config_module.ExternalDirectoryPermissionConfig,
    )
    policy_config = cast(Callable[..., object], config_module.ExternalDirectoryPolicy)

    outside_root = tmp_path.parent / "external-read-fixture"
    outside_root.mkdir(parents=True, exist_ok=True)
    outside_file = outside_root / "ref.txt"
    outside_file.write_text("external\n", encoding="utf-8")

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode="allow",
                    permission=permission_config(
                        read=policy_config(rules=(("*", "ask"),)),
                        write=policy_config(rules=(("*", "deny"),)),
                    ),
                ),
                graph=_SingleToolGraph(
                    "read_file",
                    {"filePath": str(outside_file)},
                ),
                permission_policy=policy,
            ),
        ),
    )

    waiting = runtime.run(runtime_request(prompt="external read", session_id="external-read-ask"))
    assert waiting.session.status == "waiting"
    approval_event = waiting.events[-1]
    assert approval_event.event_type == "runtime.approval_requested"
    assert approval_event.payload["tool"] == "read_file"
    assert approval_event.payload["path_scope"] == "external"
    assert approval_event.payload["operation_class"] == "read"
    assert approval_event.payload["matched_rule"] == "*"
    assert approval_event.payload["policy_surface"] == "external_directory_read"
    assert approval_event.payload["canonical_path"] == str(outside_file.resolve())


def test_external_permission_unknown_user_tilde_rule_falls_back_to_later_rule(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")

    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_config = cast(
        Callable[..., object],
        config_module.ExternalDirectoryPermissionConfig,
    )
    policy_config = cast(Callable[..., object], config_module.ExternalDirectoryPolicy)

    outside_root = tmp_path.parent / "external-tilde-fallback-fixture"
    outside_root.mkdir(parents=True, exist_ok=True)
    outside_file = outside_root / "ref.txt"
    outside_file.write_text("external\n", encoding="utf-8")

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode="allow",
                    permission=permission_config(
                        read=policy_config(
                            rules=(("~voidcode_unknown_user_for_test/**", "allow"), ("*", "ask"))
                        ),
                    ),
                ),
                graph=_SingleToolGraph("read_file", {"filePath": str(outside_file)}),
                permission_policy=policy,
            ),
        ),
    )

    waiting = runtime.run(
        runtime_request(prompt="external read", session_id="external-tilde-fallback")
    )
    approval_event = waiting.events[-1]

    assert waiting.session.status == "waiting"
    assert approval_event.event_type == "runtime.approval_requested"
    assert approval_event.payload["matched_rule"] == "*"
    assert approval_event.payload["policy_surface"] == "external_directory_read"
    assert approval_event.payload["canonical_path"] == str(outside_file.resolve())


def test_runtime_denies_external_write_when_permission_rule_denies(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")

    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_config = cast(
        Callable[..., object],
        config_module.ExternalDirectoryPermissionConfig,
    )
    policy_config = cast(Callable[..., object], config_module.ExternalDirectoryPolicy)

    outside_root = tmp_path.parent / "external-write-fixture"
    outside_root.mkdir(parents=True, exist_ok=True)
    outside_file = outside_root / "danger.txt"

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode="allow",
                    permission=permission_config(
                        read=policy_config(rules=(("*", "ask"),)),
                        write=policy_config(rules=(("*", "deny"),)),
                    ),
                ),
                graph=_SingleToolGraph(
                    "write_file",
                    {"path": str(outside_file), "content": "blocked"},
                ),
                permission_policy=policy,
            ),
        ),
    )

    denied = runtime.run(runtime_request(prompt="external write", session_id="external-write-deny"))
    assert denied.session.status == "failed"
    assert denied.events[-2].event_type == "runtime.approval_resolved"
    assert denied.events[-2].payload["decision"] == "deny"
    assert denied.events[-2].payload["path_scope"] == "external"
    assert denied.events[-2].payload["operation_class"] == "write"
    assert denied.events[-2].payload["matched_rule"] == "*"
    assert denied.events[-2].payload["policy_surface"] == "external_directory_write"
    assert denied.events[-2].payload["canonical_path"] == str(outside_file.resolve())
    assert outside_file.exists() is False


def test_runtime_denies_shell_exec_external_write_when_permission_rule_denies(
    tmp_path: Path,
) -> None:
    if sys.platform.startswith("win"):
        pytest.skip("POSIX shell redirection syntax is required for this regression")

    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")

    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_config = cast(
        Callable[..., object],
        config_module.ExternalDirectoryPermissionConfig,
    )
    policy_config = cast(Callable[..., object], config_module.ExternalDirectoryPolicy)

    outside_root = tmp_path.parent / "external-shell-write-fixture"
    outside_root.mkdir(parents=True, exist_ok=True)
    outside_file = outside_root / "out.txt"

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode="allow",
                    permission=permission_config(
                        read=policy_config(rules=(("*", "allow"),)),
                        write=policy_config(rules=(("*", "deny"),)),
                    ),
                ),
                graph=_SingleToolGraph(
                    "shell_exec",
                    {"command": f"printf blocked > {shlex.quote(str(outside_file))}"},
                ),
                permission_policy=policy,
            ),
        ),
    )

    denied = runtime.run(
        runtime_request(prompt="external shell write", session_id="external-shell-write-deny")
    )
    assert denied.session.status == "failed"
    assert denied.events[-2].event_type == "runtime.approval_resolved"
    assert denied.events[-2].payload["decision"] == "deny"
    assert denied.events[-2].payload["path_scope"] == "external"
    assert denied.events[-2].payload["operation_class"] == "execute"
    assert denied.events[-2].payload["matched_rule"] == "*"
    assert denied.events[-2].payload["policy_surface"] == "external_directory_write"
    assert denied.events[-2].payload["canonical_path"] == str(outside_file.resolve())
    assert outside_file.exists() is False


def test_runtime_denies_when_any_external_path_in_patch_is_denied(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")

    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_config = cast(
        Callable[..., object],
        config_module.ExternalDirectoryPermissionConfig,
    )
    policy_config = cast(Callable[..., object], config_module.ExternalDirectoryPolicy)

    allowed_root = tmp_path.parent / "external-allowed"
    denied_root = tmp_path.parent / "external-denied"
    allowed_root.mkdir(parents=True, exist_ok=True)
    denied_root.mkdir(parents=True, exist_ok=True)
    allowed_file = allowed_root / "ok.txt"
    denied_file = denied_root / "no.txt"

    patch_text = "\n".join(
        [
            "*** Begin Patch",
            f"*** Add File: {allowed_file}",
            "+allowed",
            f"*** Add File: {denied_file}",
            "+denied",
            "*** End Patch",
        ]
    )

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode="allow",
                    permission=permission_config(
                        read=policy_config(rules=(("*", "ask"),)),
                        write=policy_config(
                            rules=((f"{allowed_root.as_posix()}/**", "allow"), ("*", "deny"))
                        ),
                    ),
                ),
                graph=_SingleToolGraph("apply_patch", {"patch": patch_text}),
                permission_policy=policy,
            ),
        ),
    )

    denied = runtime.run(runtime_request(prompt="mixed patch", session_id="external-patch-mixed"))
    assert denied.session.status == "failed"
    assert denied.events[-2].event_type == "runtime.approval_resolved"
    assert denied.events[-2].payload["decision"] == "deny"
    assert denied.events[-2].payload["policy_surface"] == "external_directory_write"
    assert denied.events[-2].payload["matched_rule"] == "*"
    assert denied_file.exists() is False


def test_external_permission_rule_supports_tilde_home_pattern(tmp_path: Path) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")

    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="allow")
    runtime_config = cast(Callable[..., object], config_module.RuntimeConfig)
    permission_config = cast(
        Callable[..., object],
        config_module.ExternalDirectoryPermissionConfig,
    )
    policy_config = cast(Callable[..., object], config_module.ExternalDirectoryPolicy)

    home_file = Path.home() / "voidcode-ext-home-test.txt"
    home_file.write_text("home", encoding="utf-8")

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(
                    approval_mode="allow",
                    permission=permission_config(
                        read=policy_config(rules=(("~/**", "allow"), ("*", "deny"))),
                        write=policy_config(rules=(("*", "deny"),)),
                    ),
                ),
                graph=_SingleToolGraph("read_file", {"filePath": str(home_file)}),
                permission_policy=policy,
            ),
        ),
    )

    try:
        result = runtime.run(runtime_request(prompt="home read", session_id="external-home-read"))
        assert result.session.status == "completed"
        assert result.output == "done"
    finally:
        if home_file.exists():
            home_file.unlink()


def test_runtime_executes_deterministic_graph_and_emits_events(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\nbeta\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    runtime = runtime_class(workspace=tmp_path)
    result = runtime.run(runtime_request(prompt="read sample.txt"))

    assert [event.event_type for event in result.events] == [
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
    ]
    assert result.events[1].payload["skills"] == []
    assert result.events[1].payload["selected_skills"] == []
    assert result.events[1].payload["catalog_context_length"] == 0
    assert result.session.status == "completed"
    assert result.output == "alpha\nbeta"
    runtime_config = cast(dict[str, object], result.session.metadata["runtime_config"])
    assert runtime_config["execution_engine"] == "deterministic"
    assert "model" not in runtime_config


def test_provider_runtime_executes_read_path_and_persists_config(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\nbeta\n", encoding="utf-8")
    runtime_request, runtime = _provider_runtime(tmp_path, mode="allow")

    result = runtime.run(runtime_request(prompt="read sample.txt", session_id="single-agent-read"))
    replay = runtime.resume("single-agent-read")

    assert result.session.status == "completed"
    assert result.output == "alpha\nbeta"
    assert set(result.session.metadata) == {
        "workspace",
        "runtime_config",
        "runtime_state",
        "context_window",
    }
    runtime_config_payload = cast(dict[str, object], result.session.metadata["runtime_config"])
    agent_payload = runtime_config_payload.pop("agent")
    assert isinstance(agent_payload, dict)
    assert agent_payload["preset"] == "leader"
    assert runtime_config_payload == {
        "approval_mode": "allow",
        "execution_engine": "provider",
        "max_steps": None,
        "lsp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "mcp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "model": "opencode/gpt-5.4",
        "provider_fallback": None,
        "resolved_provider": {
            "active_target": {
                "raw_model": "opencode/gpt-5.4",
                "provider": "opencode",
                "model": "gpt-5.4",
            },
            "targets": [
                {
                    "raw_model": "opencode/gpt-5.4",
                    "provider": "opencode",
                    "model": "gpt-5.4",
                }
            ],
        },
        "tool_timeout_seconds": None,
    }
    runtime_state = cast(dict[str, object], result.session.metadata["runtime_state"])
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
    assert result.events[3].payload["mode"] == "provider"
    assert replay.output == result.output


def test_provider_runtime_converts_tool_exceptions_to_tool_error_results(
    tmp_path: Path,
) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\n", encoding="utf-8")
    runtime_request, runtime = _provider_runtime(tmp_path, mode="allow")
    read_file_module = importlib.import_module("voidcode.tools.read_file")
    read_file_tool = cast(ReadFileToolType, read_file_module.ReadFileTool)

    def _failing_invoke(_self: object, _call: object, *, workspace: Path) -> object:
        _ = workspace
        raise ValueError("provider tool boom")

    with patch.object(read_file_tool, "invoke", autospec=True, side_effect=_failing_invoke):
        result = runtime.run(
            runtime_request(prompt="read sample.txt", session_id="provider-tool-error")
        )

    assert result.session.status == "completed"
    tool_completed = next(
        event for event in result.events if event.event_type == "runtime.tool_completed"
    )
    assert tool_completed.payload["status"] == "error"
    assert tool_completed.payload["error"] == "provider tool boom"


def test_provider_runtime_requests_and_resumes_write_approval(tmp_path: Path) -> None:
    runtime_request, runtime = _provider_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(
            prompt="write danger.txt approved later", session_id="single-agent-approval"
        )
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    resumed = runtime.resume(
        "single-agent-approval",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert waiting.session.status == "waiting"
    assert waiting.events[3].payload["mode"] == "provider"
    assert waiting.events[-1].event_type == "runtime.approval_requested"
    assert resumed.session.status == "completed"
    assert resumed.output == "Wrote file successfully: danger.txt"
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved later"


def test_runtime_uses_repo_local_config_to_allow_write_requests_without_explicit_policy(
    tmp_path: Path,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    (tmp_path / ".voidcode.json").write_text(
        json.dumps({"approval_mode": "allow"}),
        encoding="utf-8",
    )

    runtime = runtime_class(workspace=tmp_path)
    result = runtime.run(
        runtime_request(
            prompt="write configured.txt config file approved", session_id="config-file"
        )
    )

    assert result.session.status == "completed"
    assert result.events[6].event_type == "runtime.approval_resolved"
    assert result.events[6].payload["decision"] == "allow"
    assert (tmp_path / "configured.txt").read_text(encoding="utf-8") == "config file approved"


def test_runtime_uses_environment_config_to_allow_write_requests_without_code_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_request, runtime_class = _load_runtime_types()
    monkeypatch.setenv("VOIDCODE_APPROVAL_MODE", "allow")

    runtime = runtime_class(workspace=tmp_path)
    result = runtime.run(
        runtime_request(prompt="write env.txt env approved", session_id="env-file")
    )

    assert result.session.status == "completed"
    assert result.events[6].event_type == "runtime.approval_resolved"
    assert result.events[6].payload["decision"] == "allow"
    assert (tmp_path / "env.txt").read_text(encoding="utf-8") == "env approved"


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
    leader_task = first_runtime.start_background_task(
        runtime_request(prompt="read sample.txt", parent_session_id="leader-session")
    )
    _ = first_runtime.start_background_task(runtime_request(prompt="read sample.txt"))

    deadline = time.time() + 2
    while time.time() < deadline:
        current = first_runtime.load_background_task(leader_task.task.id)
        if current.status in ("completed", "failed", "cancelled", "interrupted"):
            break
        time.sleep(0.01)

    second_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    listed = second_runtime.list_background_tasks_by_parent_session(
        parent_session_id="leader-session"
    )

    assert len(listed) == 1
    assert listed[0].task.id == leader_task.task.id
    assert listed[0].prompt == "read sample.txt"


def test_runtime_background_task_cancel_reconciles_orphaned_task_from_fresh_runtime(
    tmp_path: Path,
) -> None:
    _, runtime_class = _load_runtime_types()
    task_module = importlib.import_module("voidcode.runtime.task")
    storage_module = importlib.import_module("voidcode.runtime.storage")

    first_runtime = cast(RuntimeRunner, cast(object, runtime_class(workspace=tmp_path)))
    _ = first_runtime
    store = cast(SessionStoreLike, storage_module.SqliteSessionStore())
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


def test_runtime_executes_grep_deterministic_graph_and_emits_events(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\nbeta alpha\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    runtime = runtime_class(workspace=tmp_path)
    result = runtime.run(runtime_request(prompt="grep alpha sample.txt"))

    assert [event.event_type for event in result.events] == [
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
    ]
    assert result.events[4].payload == {
        "tool": "grep",
        "arguments": {"pattern": "alpha", "path": "sample.txt"},
        "path": "sample.txt",
    }
    assert result.events[8].payload == {
        "tool": "grep",
        "tool_call_id": ANY,
        "arguments": {"pattern": "alpha", "path": "sample.txt"},
        "status": "ok",
        "content": (
            "Found 2 match(es) for 'alpha' in sample.txt\n"
            "sample.txt:1: alpha\n"
            "sample.txt:2: beta alpha"
        ),
        "error": None,
        "path": "sample.txt",
        "pattern": "alpha",
        "regex": False,
        "context": 0,
        "match_count": 2,
        "truncated": False,
        "partial": False,
        "matches": [
            {
                "file": "sample.txt",
                "line": 1,
                "text": "alpha",
                "columns": [1],
                "before": [],
                "after": [],
            },
            {
                "file": "sample.txt",
                "line": 2,
                "text": "beta alpha",
                "columns": [6],
                "before": [],
                "after": [],
            },
        ],
        "display": {
            "kind": "search",
            "title": "Search",
            "summary": "alpha",
            "args": ["alpha", "sample.txt"],
        },
        "tool_status": {
            "invocation_id": ANY,
            "tool_name": "grep",
            "phase": "completed",
            "status": "completed",
            "label": "alpha",
            "display": {
                "kind": "search",
                "title": "Search",
                "summary": "alpha",
                "args": ["alpha", "sample.txt"],
            },
        },
    }
    assert result.session.status == "completed"
    assert result.output == (
        "Found 2 match(es) for 'alpha' in sample.txt\nsample.txt:1: alpha\nsample.txt:2: beta alpha"
    )


def test_runtime_allows_non_read_only_tool_after_explicit_resume_approval(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt approved later", session_id="approval-session")
    )

    assert waiting.session.status == "waiting"
    assert [event.event_type for event in waiting.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    resumed = runtime.resume(
        "approval-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert [event.event_type for event in resumed.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert resumed.output == "Wrote file successfully: danger.txt"
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved later"


def test_runtime_approved_resume_persists_failure_when_pending_tool_is_missing(
    tmp_path: Path,
) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write drift.txt unavailable later", session_id="drift-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    _, runtime_class = _load_runtime_types()
    service_module = importlib.import_module("voidcode.runtime.service")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    tool_registry_class = cast(ToolRegistryClassLike, service_module.ToolRegistry)
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                tool_registry=tool_registry_class.with_defaults().excluding(["write_file"]),
                permission_policy=permission_policy(mode="ask"),
            ),
        ),
    )

    resumed = resumed_runtime.resume(
        "drift-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )
    replay = resumed_runtime.resume("drift-session")
    sessions = resumed_runtime.list_sessions()

    assert resumed.session.status == "failed"
    assert [event.event_type for event in resumed.events] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert [event.sequence for event in resumed.events] == list(range(1, 10))
    assert resumed.events[-1].payload == {"error": "unknown tool: write_file"}
    assert replay.session.status == "failed"
    assert [(event.sequence, event.event_type, event.payload) for event in replay.events] == [
        (event.sequence, event.event_type, event.payload) for event in resumed.events
    ]
    assert sessions[0].status == "failed"
    assert (tmp_path / "drift.txt").exists() is False


def test_runtime_resumed_approval_renumbers_fixed_finalize_sequences(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt renumbered", session_id="approval-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    resumed = runtime.resume(
        "approval-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert [event.sequence for event in resumed.events] == list(range(1, 13))
    assert resumed.events[-1].event_type == "graph.response_ready"


def test_runtime_persists_pending_approval_until_single_resume_resolution(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")

    waiting = runtime.run(
        runtime_request(
            prompt="write danger.txt persisted approval", session_id="persisted-approval"
        )
    )
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

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt stale replay", session_id="stale-replay-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])
    resolved = runtime.resume(
        "stale-replay-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        approval_event = next(
            event for event in resolved.events if event.event_type == "runtime.approval_requested"
        )
        _ = connection.execute(
            (
                "UPDATE sessions SET pending_approval_json = ?, resume_checkpoint_json = ? "
                "WHERE session_id = ?"
            ),
            (
                json.dumps(
                    {
                        "request_id": approval_request_id,
                        "tool_name": "write_file",
                        "arguments": {"path": "danger.txt", "content": "stale replay"},
                        "target_summary": "write_file danger.txt",
                        "reason": "non-read-only tool invocation",
                        "policy_mode": "ask",
                        "request_event_sequence": approval_event.sequence,
                        "owner_session_id": "stale-replay-session",
                        "owner_parent_session_id": None,
                        "delegated_task_id": None,
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
    """Graph that returns different write_file arguments on consecutive steps."""

    def __init__(self) -> None:
        self._call_count = 0

    def step(
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
            return _GraphStep(
                events=(),
                tool_call=cast(
                    ToolCallFactory,
                    importlib.import_module("voidcode.tools.contracts").ToolCall,
                )(
                    tool_name="write_file",
                    arguments={
                        "path": "divergent.txt",
                        "content": f"body-{suffix}",
                    },
                ),
            )
        return _GraphStep(events=(), tool_call=None, output="written", is_finished=True)


def test_runtime_approval_resume_executes_original_pending_tool_when_graph_would_diverge(
    tmp_path: Path,
) -> None:
    """Approval resume must execute the persisted pending tool before asking
    the graph/provider for another step, even if the provider would diverge."""
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    permission_policy = cast(Callable[..., object], permission_module.PermissionPolicy)
    policy = permission_policy(mode="ask")
    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                graph=_DivergentWriteFileGraph(),
                permission_policy=policy,
            ),
        ),
    )

    waiting = runtime.run(
        runtime_request(prompt="write divergent.txt", session_id="divergent-approval")
    )
    assert waiting.session.status == "waiting"
    assert waiting.events[-1].event_type == "runtime.approval_requested"
    original_request_id = cast(str, waiting.events[-1].payload["request_id"])

    result = runtime.resume(
        "divergent-approval",
        approval_request_id=original_request_id,
        approval_decision="allow",
    )
    assert result.session.status == "completed"
    assert result.output == "written"
    assert [event.event_type for event in result.events].count("runtime.approval_requested") == 1
    assert [event.event_type for event in result.events].count("runtime.approval_resolved") == 1
    assert [event.event_type for event in result.events].count("runtime.tool_completed") == 1
    assert result.events[-1].event_type == "runtime.tool_completed"
    assert result.events[-1].payload["tool"] == "write_file"
    completed_arguments = cast(dict[str, object], result.events[-1].payload["arguments"])
    assert completed_arguments["path"] == "divergent.txt"
    assert (tmp_path / "divergent.txt").read_text(encoding="utf-8") == "body-first"


def test_runtime_resumes_multi_step_loop_with_approval_and_stable_replay(tmp_path: Path) -> None:
    _ = (tmp_path / "source.txt").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(runtime_request(prompt=_multi_step_prompt(), session_id="loop-session"))
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    resumed = runtime.resume(
        "loop-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )
    replay = runtime.resume("loop-session")
    sessions = runtime.list_sessions()

    assert waiting.session.status == "waiting"
    assert waiting.output is None
    assert [event.event_type for event in waiting.events] == [
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
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]
    assert [event.sequence for event in waiting.events] == list(range(1, 15))
    assert waiting.events[11].payload == {
        "tool": "write_file",
        "arguments": {"path": "copied.txt", "content": "copied marker"},
        "path": "copied.txt",
    }

    assert resumed.session.status == "completed"
    assert [event.event_type for event in resumed.events] == [
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
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert [event.sequence for event in resumed.events] == list(range(1, 27))
    assert resumed.output == (
        "Found 1 match(es) for 'copied' in copied.txt\ncopied.txt:1: copied marker"
    )
    assert replay.output == resumed.output
    assert [event.sequence for event in replay.events] == list(range(1, 27))
    assert [(event.sequence, event.event_type, event.payload) for event in replay.events] == [
        (event.sequence, event.event_type, event.payload) for event in resumed.events
    ]
    assert resumed.events[23].payload == {
        "tool": "grep",
        "tool_call_id": ANY,
        "arguments": {"pattern": "copied", "path": "copied.txt"},
        "status": "ok",
        "content": ("Found 1 match(es) for 'copied' in copied.txt\ncopied.txt:1: copied marker"),
        "error": None,
        "path": "copied.txt",
        "pattern": "copied",
        "regex": False,
        "context": 0,
        "match_count": 1,
        "truncated": False,
        "partial": False,
        "matches": [
            {
                "file": "copied.txt",
                "line": 1,
                "text": "copied marker",
                "columns": [1],
                "before": [],
                "after": [],
            }
        ],
        "display": {
            "kind": "search",
            "title": "Search",
            "summary": "copied",
            "args": ["copied", "copied.txt"],
        },
        "tool_status": {
            "invocation_id": ANY,
            "tool_name": "grep",
            "phase": "completed",
            "status": "completed",
            "label": "copied",
            "display": {
                "kind": "search",
                "title": "Search",
                "summary": "copied",
                "args": ["copied", "copied.txt"],
            },
        },
    }
    assert [event.event_type for event in resumed.events].count("runtime.approval_requested") == 1
    assert [event.event_type for event in resumed.events].count("runtime.approval_resolved") == 1
    assert [summary.session.id for summary in sessions] == ["loop-session"]
    assert sessions[0].status == "completed"
    assert sessions[0].prompt == _multi_step_prompt()
    assert sessions[0].updated_at == 2
    assert (tmp_path / "copied.txt").read_text(encoding="utf-8") == "copied marker"


def test_runtime_denied_multi_step_loop_stops_before_follow_up_tools(tmp_path: Path) -> None:
    _ = (tmp_path / "source.txt").write_text("alpha\nbeta alpha\n", encoding="utf-8")
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt=_multi_step_prompt(), session_id="deny-loop-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    denied = runtime.resume(
        "deny-loop-session",
        approval_request_id=approval_request_id,
        approval_decision="deny",
    )
    replay = runtime.resume("deny-loop-session")
    sessions = runtime.list_sessions()

    assert waiting.session.status == "waiting"
    assert waiting.output is None
    assert [event.event_type for event in waiting.events] == [
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
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]

    assert denied.session.status == "failed"
    assert [event.event_type for event in denied.events] == [
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
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert [event.sequence for event in denied.events] == list(range(1, 17))
    assert denied.events[-1].payload == {"error": "permission denied for tool: write_file"}
    assert denied.output is None
    assert replay.output is None
    assert [(event.sequence, event.event_type, event.payload) for event in replay.events] == [
        (event.sequence, event.event_type, event.payload) for event in denied.events
    ]
    assert [event.event_type for event in denied.events].count("graph.tool_request_created") == 2
    assert "grep" not in [
        cast(str, event.payload.get("tool"))
        for event in denied.events
        if event.event_type == "graph.tool_request_created"
    ]
    assert [summary.session.id for summary in sessions] == ["deny-loop-session"]
    assert sessions[0].status == "failed"
    assert sessions[0].updated_at == 2
    assert (tmp_path / "copied.txt").exists() is False


def test_runtime_rejects_stale_session_schema_for_pending_approval(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    try:
        _ = connection.execute(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                workspace TEXT NOT NULL,
                status TEXT NOT NULL,
                turn INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                output TEXT,
                metadata_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_event_sequence INTEGER NOT NULL
            )
            """
        )
        _ = connection.execute(
            """
            CREATE TABLE session_events (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence)
            )
            """
        )
        _ = connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="sqlite runtime schema mismatch"):
        _ = runtime.run(
            runtime_request(prompt="write danger.txt stale approval", session_id="stale-session")
        )


def test_runtime_replay_is_unchanged_when_resume_checkpoint_exists(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt replay checkpoint", session_id="checkpoint-replay")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    resumed = runtime.resume(
        "checkpoint-replay",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )
    replay = runtime.resume("checkpoint-replay")

    assert resumed.session.status == "completed"
    assert replay.output == resumed.output
    assert [(event.sequence, event.event_type, event.payload) for event in replay.events] == [
        (event.sequence, event.event_type, event.payload) for event in resumed.events
    ]


def test_runtime_resume_uses_persisted_runtime_config_over_fresh_resume_overrides(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps({"approval_mode": "deny", "model": "repo/model"}),
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
                config=load_runtime_config(tmp_path, approval_mode="allow", model="session/model"),
                permission_policy=cast(Callable[..., object], permission_module.PermissionPolicy)(
                    mode="allow"
                ),
            ),
        ),
    )
    _ = (tmp_path / "sample.txt").write_text("resume config\n", encoding="utf-8")

    _ = initial_runtime.run(
        runtime_request(prompt="read sample.txt", session_id="resume-config-session")
    )

    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=runtime_config(approval_mode="deny", model="fresh/model"),
                permission_policy=cast(Callable[..., object], permission_module.PermissionPolicy)(
                    mode="deny"
                ),
            ),
        ),
    )
    replay = resumed_runtime.resume("resume-config-session")

    assert set(replay.session.metadata) == {
        "workspace",
        "runtime_config",
        "runtime_state",
        "context_window",
    }
    assert replay.session.metadata["runtime_config"] == {
        "approval_mode": "allow",
        "execution_engine": "deterministic",
        "max_steps": None,
        "tool_timeout_seconds": None,
        "lsp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "mcp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "model": "session/model",
        "provider_fallback": None,
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


def test_runtime_persists_reasoning_effort_in_runtime_config_and_preserves_on_resume(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps({"reasoning_effort": "low"}),
        encoding="utf-8",
    )
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    load_runtime_config = cast(Callable[..., object], config_module.load_runtime_config)

    initial_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=load_runtime_config(tmp_path, reasoning_effort="high"),
                permission_policy=cast(Callable[..., object], permission_module.PermissionPolicy)(
                    mode="allow"
                ),
            ),
        ),
    )
    _ = (tmp_path / "sample.txt").write_text("reasoning effort\n", encoding="utf-8")

    initial = initial_runtime.run(
        runtime_request(prompt="read sample.txt", session_id="reasoning-effort-session")
    )

    initial_runtime_config = cast(dict[str, object], initial.session.metadata["runtime_config"])
    assert initial_runtime_config["reasoning_effort"] == "high"

    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=load_runtime_config(tmp_path, reasoning_effort="medium"),
                permission_policy=cast(Callable[..., object], permission_module.PermissionPolicy)(
                    mode="allow"
                ),
            ),
        ),
    )
    replay = resumed_runtime.resume("reasoning-effort-session")
    replay_runtime_config = cast(dict[str, object], replay.session.metadata["runtime_config"])

    assert replay_runtime_config["reasoning_effort"] == "high"


def test_runtime_request_metadata_reasoning_effort_overrides_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".voidcode.json"
    config_path.write_text(
        json.dumps({"reasoning_effort": "low"}),
        encoding="utf-8",
    )
    runtime_request, runtime_class = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    load_runtime_config = cast(Callable[..., object], config_module.load_runtime_config)

    runtime = cast(
        RuntimeRunner,
        cast(
            object,
            runtime_class(
                workspace=tmp_path,
                config=load_runtime_config(tmp_path),
                permission_policy=cast(Callable[..., object], permission_module.PermissionPolicy)(
                    mode="allow"
                ),
            ),
        ),
    )
    _ = (tmp_path / "sample.txt").write_text("override\n", encoding="utf-8")

    response = runtime.run(
        runtime_request(
            prompt="read sample.txt",
            session_id="reasoning-effort-override-session",
            metadata={"reasoning_effort": "high"},
        )
    )

    runtime_config_metadata = cast(dict[str, object], response.session.metadata["runtime_config"])
    assert runtime_config_metadata["reasoning_effort"] == "high"


def test_runtime_denies_non_read_only_tool_on_resume(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt denied on resume", session_id="approval-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    denied = runtime.resume(
        "approval-session",
        approval_request_id=approval_request_id,
        approval_decision="deny",
    )

    assert denied.session.status == "failed"
    assert [event.event_type for event in denied.events[-2:]] == [
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert denied.output is None
    assert (tmp_path / "danger.txt").exists() is False


def test_runtime_marks_resumed_approval_failure_and_clears_pending_request(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt resume failure", session_id="approval-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    write_file_module = importlib.import_module("voidcode.tools.write_file")
    write_tool = cast(ReadFileToolType, write_file_module.WriteFileTool)

    def _failing_write_invoke(_self: object, _call: object, *, workspace: Path) -> object:
        _ = workspace
        raise RuntimeError("resume boom")

    with patch.object(write_tool, "invoke", autospec=True, side_effect=_failing_write_invoke):
        resumed_runtime_class = _load_runtime_types()[1]
        resumed_runtime = cast(
            RuntimeRunner,
            cast(
                object,
                resumed_runtime_class(
                    workspace=tmp_path,
                    permission_policy=policy,
                ),
            ),
        )
        resumed = resumed_runtime.resume(
            "approval-session",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )

    assert resumed.session.status == "failed"
    assert resumed.events[-1].event_type == "runtime.failed"
    assert resumed.events[-1].payload["error"] == "resume boom"

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
    assert replay.session.status == "failed"


def test_runtime_preserves_pending_request_when_resumed_finalize_raises(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt finalize failure", session_id="approval-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    class FailingFinalizeGraph:
        def step(
            self, request: object, tool_results: tuple[object, ...], *, session: object
        ) -> object:
            if not tool_results:
                return _GraphStep(
                    events=(),
                    tool_call=cast(
                        ToolCallFactory,
                        importlib.import_module("voidcode.tools.contracts").ToolCall,
                    )(
                        tool_name="write_file",
                        arguments={"path": "danger.txt", "content": "finalize failure"},
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
                graph=FailingFinalizeGraph(),
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
                graph=FailingFinalizeGraph(),
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

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt save failure", session_id="approval-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    storage_module = importlib.import_module("voidcode.runtime.storage")
    sqlite_store_class = cast(Callable[[], SessionStoreLike], storage_module.SqliteSessionStore)
    base_store = sqlite_store_class()

    class FailingTerminalSaveStore:
        def save_run(
            self,
            *,
            workspace: Path,
            request: object,
            response: object,
            clear_pending_approval: bool = True,
        ) -> None:
            _ = request
            if clear_pending_approval:
                raise RuntimeError("save boom")
            base_store.save_run(
                workspace=workspace,
                request=cast(RuntimeRequestLike, request),
                response=cast(RuntimeResponseLike, response),
                clear_pending_approval=clear_pending_approval,
            )

        def list_sessions(self, *, workspace: Path) -> tuple[object, ...]:
            return base_store.list_sessions(workspace=workspace)

        def load_session(self, *, workspace: Path, session_id: str) -> object:
            return base_store.load_session(workspace=workspace, session_id=session_id)

        def save_pending_approval(
            self,
            *,
            workspace: Path,
            request: object,
            response: object,
            pending_approval: object,
        ) -> None:
            base_store.save_pending_approval(
                workspace=workspace,
                request=cast(RuntimeRequestLike, request),
                response=cast(RuntimeResponseLike, response),
                pending_approval=pending_approval,
            )

        def load_pending_approval(self, *, workspace: Path, session_id: str) -> object:
            return base_store.load_pending_approval(workspace=workspace, session_id=session_id)

        def clear_pending_approval(self, *, workspace: Path, session_id: str) -> None:
            base_store.clear_pending_approval(workspace=workspace, session_id=session_id)

    resumed_runtime_class = _load_runtime_types()[1]
    resumed_runtime = cast(
        RuntimeRunner,
        cast(
            object,
            resumed_runtime_class(
                workspace=tmp_path,
                permission_policy=policy,
                session_store=FailingTerminalSaveStore(),
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

    assert replay.session.status == "waiting"
    assert replay.events[-1].event_type == "runtime.approval_requested"
    assert cast(str, replay.events[-1].payload["request_id"]) == approval_request_id


def test_cli_run_command_prints_clean_file_contents_by_default(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("slice proof\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "run",
            "read sample.txt",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert result.stdout == "slice proof\n"
    assert "LiteLLM:WARNING" in result.stderr or result.stderr == ""


def test_cli_run_command_json_outputs_events_and_file_contents(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("slice proof\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "run",
            "read sample.txt",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    payload = json.loads(result.stdout)
    event_types = [event["event_type"] for event in payload["events"]]

    assert result.returncode == 0
    assert "LiteLLM:WARNING" in result.stderr or result.stderr == ""
    assert "runtime.request_received" in event_types
    assert "runtime.tool_completed" in event_types
    assert payload["output"] == "slice proof"


def test_cli_run_command_approval_allow_writes_file_under_tty_and_replays_session(
    tmp_path: Path,
) -> None:
    session_id = "tty-approval-allow-session"
    result = _run_cli_in_tty(
        workspace=tmp_path,
        request="write approved.txt approved via tty",
        session_id=session_id,
        approval_input="y",
    )

    transcript = _normalize_terminal_text(result.stdout + result.stderr)
    written_file = tmp_path / "approved.txt"
    resume_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "resume",
            session_id,
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_test_env(),
    )

    assert result.returncode == 0
    assert "Approve write_file for write_file approved.txt? [y/N]:" in transcript
    assert "EVENT runtime.approval_requested" in transcript
    assert "EVENT runtime.approval_resolved" in transcript
    assert "decision=allow" in transcript
    assert "EVENT runtime.tool_completed" in transcript
    assert "RESULT" in transcript
    assert "approved via tty" in transcript
    assert written_file.read_text(encoding="utf-8") == "approved via tty"

    assert resume_result.returncode == 0
    assert "EVENT runtime.approval_requested" in resume_result.stdout
    assert "EVENT runtime.approval_resolved" in resume_result.stdout
    assert "approved via tty" in resume_result.stdout


def test_cli_run_command_approval_deny_blocks_write_under_tty_and_replays_failure(
    tmp_path: Path,
) -> None:
    session_id = "tty-approval-deny-session"
    result = _run_cli_in_tty(
        workspace=tmp_path,
        request="write denied.txt denied via tty",
        session_id=session_id,
        approval_input="n",
    )

    transcript = _normalize_terminal_text(result.stdout + result.stderr)
    denied_file = tmp_path / "denied.txt"
    resume_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "resume",
            session_id,
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_test_env(),
    )

    assert result.returncode == 0
    assert "Approve write_file for write_file denied.txt? [y/N]:" in transcript
    assert "EVENT runtime.approval_requested" in transcript
    assert "EVENT runtime.approval_resolved" in transcript
    assert "decision=deny" in transcript
    assert "EVENT runtime.failed" in transcript
    assert "permission denied for tool: write_file" in transcript
    assert "RESULT" in transcript
    assert denied_file.exists() is False

    assert resume_result.returncode == 0
    assert "EVENT runtime.approval_requested" in resume_result.stdout
    assert "EVENT runtime.approval_resolved" in resume_result.stdout
    assert "EVENT runtime.failed" in resume_result.stdout
    assert "permission denied for tool: write_file" in resume_result.stdout


def test_runtime_persists_and_resumes_session_across_instances(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("persisted slice\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    first_runtime = runtime_class(workspace=tmp_path)
    first_result = first_runtime.run(
        runtime_request(prompt="read sample.txt", session_id="demo-session")
    )

    second_runtime = runtime_class(workspace=tmp_path)
    sessions = second_runtime.list_sessions()
    resumed = second_runtime.resume("demo-session")

    assert [summary.session.id for summary in sessions] == ["demo-session"]
    assert first_result.output == resumed.output
    assert [event.event_type for event in resumed.events] == [
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
    ]


def test_runtime_stream_exposes_ordered_events_and_final_output(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("stream proof\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    runtime = runtime_class(workspace=tmp_path)
    stream = runtime.run_stream(runtime_request(prompt="read sample.txt"))

    chunks = list(stream)
    event_chunks = [chunk for chunk in chunks if chunk.event is not None]
    output_chunks = [chunk for chunk in chunks if chunk.kind == "output"]
    pre_finalization_chunks = chunks[:9]
    final_chunks = chunks[9:]

    assert [chunk.event.event_type for chunk in event_chunks if chunk.event is not None] == [
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
    ]
    assert [chunk.session.status for chunk in pre_finalization_chunks] == [
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
    ]
    assert [chunk.session.status for chunk in final_chunks] == [
        "completed",
        "completed",
        "completed",
    ]
    assert [chunk.output for chunk in output_chunks] == ["stream proof"]
    assert len(output_chunks) == 1


def test_runtime_stream_yields_before_tool_completion(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("delayed stream\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    read_file_module = importlib.import_module("voidcode.tools.read_file")

    tool_started = threading.Event()
    allow_tool_completion = threading.Event()
    fifth_chunk_ready = threading.Event()
    fifth_chunk: list[StreamChunkLike] = []

    read_file_tool = cast(ReadFileToolType, read_file_module.ReadFileTool)
    original_invoke = read_file_tool.invoke

    def _blocking_invoke(self: object, _call: object, *, workspace: Path) -> object:
        tool_started.set()
        _ = allow_tool_completion.wait(timeout=2)
        return original_invoke(self, _call, workspace=workspace)

    with patch.object(read_file_tool, "invoke", autospec=True, side_effect=_blocking_invoke):
        runtime = runtime_class(workspace=tmp_path)
        stream = runtime.run_stream(runtime_request(prompt="read sample.txt"))

        first_four_chunks = [next(stream) for _ in range(8)]

        assert [
            chunk.event.event_type for chunk in first_four_chunks if chunk.event is not None
        ] == [
            "runtime.request_received",
            "runtime.skills_loaded",
            "graph.loop_step",
            "graph.model_turn",
            "graph.tool_request_created",
            "runtime.tool_lookup_succeeded",
            "runtime.permission_resolved",
            "runtime.tool_started",
        ]
        assert all(chunk.session.status == "running" for chunk in first_four_chunks)

        def _consume_fifth_chunk() -> None:
            fifth_chunk.append(next(stream))
            fifth_chunk_ready.set()

        worker = threading.Thread(target=_consume_fifth_chunk)
        worker.start()

        assert tool_started.wait(timeout=0.2) is True
        time.sleep(0.05)
        assert fifth_chunk_ready.is_set() is False

        started = time.monotonic()
        allow_tool_completion.set()
        worker.join(timeout=1)
        remaining_chunks = list(stream)
        elapsed = time.monotonic() - started

        assert worker.is_alive() is False
        assert elapsed < 1
        assert [chunk.event.event_type for chunk in fifth_chunk if chunk.event is not None] == [
            "runtime.tool_completed"
        ]
        assert all(chunk.session.status == "running" for chunk in fifth_chunk)
        assert [
            chunk.event.event_type for chunk in remaining_chunks if chunk.event is not None
        ] == ["graph.loop_step", "graph.response_ready"]
        assert [chunk.output for chunk in remaining_chunks if chunk.kind == "output"] == [
            "delayed stream"
        ]
        assert all(chunk.session.status == "completed" for chunk in remaining_chunks)


def test_runtime_stream_emits_failed_terminal_chunk_before_tool_error(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("failure proof\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    read_file_module = importlib.import_module("voidcode.tools.read_file")
    read_file_tool = cast(ReadFileToolType, read_file_module.ReadFileTool)

    def _failing_invoke(_self: object, _call: object, *, workspace: Path) -> object:
        _ = workspace
        raise ValueError("boom from tool")

    with patch.object(read_file_tool, "invoke", autospec=True, side_effect=_failing_invoke):
        runtime = runtime_class(workspace=tmp_path)
        stream = runtime.run_stream(runtime_request(prompt="read sample.txt"))

        first_four_chunks = [next(stream) for _ in range(8)]
        tool_completed_chunk = next(stream)
        failed_chunk = next(stream)
        with pytest.raises(ValueError, match="boom from tool"):
            list(stream)

    assert [chunk.event.event_type for chunk in first_four_chunks if chunk.event is not None] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_started",
    ]
    assert all(chunk.session.status == "running" for chunk in first_four_chunks)
    assert tool_completed_chunk.kind == "event"
    assert tool_completed_chunk.event is not None
    assert tool_completed_chunk.event.event_type == "runtime.tool_completed"
    assert tool_completed_chunk.event.payload["tool"] == "read_file"
    assert tool_completed_chunk.event.payload["status"] == "error"
    assert tool_completed_chunk.event.payload["error"] == "boom from tool"
    tool_status = tool_completed_chunk.event.payload["tool_status"]
    assert isinstance(tool_status, dict)
    typed_tool_status = cast(dict[str, object], tool_status)
    assert typed_tool_status["tool_name"] == "read_file"
    assert typed_tool_status["status"] == "failed"
    assert tool_completed_chunk.session.status == "running"
    assert failed_chunk.kind == "event"
    assert failed_chunk.event is not None
    assert failed_chunk.event.event_type == "runtime.failed"
    assert failed_chunk.event.payload["error"] == "boom from tool"
    assert failed_chunk.session.status == "failed"


def test_runtime_resume_stream_yields_incrementally_before_resumed_tool_completion(
    tmp_path: Path,
) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write delayed.txt resumed later", session_id="resume-stream")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    write_file_module = importlib.import_module("voidcode.tools.write_file")
    write_tool = cast(ReadFileToolType, write_file_module.WriteFileTool)
    original_invoke = write_tool.invoke
    tool_started = threading.Event()
    allow_tool_completion = threading.Event()
    first_chunks_ready = threading.Event()
    blocked_chunk_ready = threading.Event()
    first_chunks: list[StreamChunkLike] = []
    blocked_chunk: list[StreamChunkLike] = []

    def _blocking_invoke(self: object, _call: object, *, workspace: Path) -> object:
        tool_started.set()
        _ = allow_tool_completion.wait(timeout=2)
        return original_invoke(self, _call, workspace=workspace)

    with patch.object(write_tool, "invoke", autospec=True, side_effect=_blocking_invoke):
        resumed_runtime_request, resumed_runtime = _approval_runtime(tmp_path, mode="ask")
        _ = resumed_runtime_request
        stream = resumed_runtime.resume_stream(
            "resume-stream",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )

        def _consume_first_chunks() -> None:
            for _ in range(2):
                first_chunks.append(next(stream))
            first_chunks_ready.set()

        first_worker = threading.Thread(target=_consume_first_chunks)
        first_worker.start()
        first_worker.join(timeout=1)

        assert first_worker.is_alive() is False
        assert first_chunks_ready.is_set() is True
        assert tool_started.is_set() is False
        assert [chunk.event.event_type for chunk in first_chunks if chunk.event is not None] == [
            "runtime.approval_resolved",
            "runtime.tool_started",
        ]
        assert all(chunk.session.status == "running" for chunk in first_chunks)

        def _consume_blocked_chunk() -> None:
            blocked_chunk.append(next(stream))
            blocked_chunk_ready.set()

        second_worker = threading.Thread(target=_consume_blocked_chunk)
        second_worker.start()

        assert tool_started.wait(timeout=0.2) is True
        time.sleep(0.05)
        assert blocked_chunk_ready.is_set() is False

        started = time.monotonic()
        allow_tool_completion.set()
        second_worker.join(timeout=1)
        remaining_chunks = list(stream)
        elapsed = time.monotonic() - started

        assert second_worker.is_alive() is False
        assert elapsed < 1
        assert [chunk.event.event_type for chunk in blocked_chunk if chunk.event is not None] == [
            "runtime.tool_completed"
        ]
        assert all(chunk.session.status == "running" for chunk in blocked_chunk)
        assert [
            chunk.event.event_type for chunk in remaining_chunks if chunk.event is not None
        ] == [
            "graph.loop_step",
            "graph.response_ready",
        ]
        assert [chunk.output for chunk in remaining_chunks if chunk.kind == "output"] == [
            "Wrote file successfully: delayed.txt"
        ]
        assert all(chunk.session.status == "completed" for chunk in remaining_chunks)


def test_runtime_resume_stream_reconstructs_replayed_chunk_statuses(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("replay proof\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    completed_runtime = runtime_class(workspace=tmp_path)
    _ = completed_runtime.run(
        runtime_request(prompt="read sample.txt", session_id="completed-stream")
    )
    completed_chunks = list(completed_runtime.resume_stream("completed-stream"))

    assert [chunk.event.event_type for chunk in completed_chunks if chunk.event is not None] == [
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
    ]
    assert [chunk.session.status for chunk in completed_chunks[:9]] == [
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
    ]
    assert [chunk.session.status for chunk in completed_chunks[9:]] == [
        "completed",
        "completed",
        "completed",
    ]

    approval_runtime_request, approval_runtime = _approval_runtime(tmp_path, mode="ask")
    waiting = approval_runtime.run(
        approval_runtime_request(
            prompt="write waiting.txt pending replay", session_id="waiting-stream"
        )
    )
    waiting_chunks = list(approval_runtime.resume_stream("waiting-stream"))

    assert [chunk.event.event_type for chunk in waiting_chunks if chunk.event is not None] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]
    assert [chunk.session.status for chunk in waiting_chunks[:-1]] == [
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
    ]
    assert waiting_chunks[-1].session.status == "waiting"

    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])
    _ = approval_runtime.resume(
        "waiting-stream",
        approval_request_id=approval_request_id,
        approval_decision="deny",
    )
    failed_chunks = list(approval_runtime.resume_stream("waiting-stream"))

    assert [chunk.event.event_type for chunk in failed_chunks[-3:] if chunk.event is not None] == [
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert [chunk.session.status for chunk in failed_chunks[-3:]] == [
        "waiting",
        "running",
        "failed",
    ]


def test_cli_lists_and_resumes_persisted_session(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("resume proof\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    first_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "run",
            "read sample.txt",
            "--workspace",
            str(tmp_path),
            "--session-id",
            "demo-session",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    list_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "list",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    resume_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "resume",
            "demo-session",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert first_result.returncode == 0
    assert list_result.returncode == 0
    assert resume_result.returncode == 0
    assert "SESSION id=demo-session status=completed" in list_result.stdout
    assert "RESULT" in resume_result.stdout
    assert "resume proof" in resume_result.stdout
