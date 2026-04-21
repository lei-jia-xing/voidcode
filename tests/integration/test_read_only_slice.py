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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import patch

import pytest


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
    session: object
    status: str
    metadata: dict[str, object]


class SessionRefLike(Protocol):
    id: str


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


def _single_agent_runtime(
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
                    execution_engine="single_agent",
                    model="opencode/gpt-5.4",
                ),
                permission_policy=policy,
            ),
        ),
    )
    return runtime_request, runtime


def test_single_agent_runtime_surfaces_provider_context_limit_failure_kind(tmp_path: Path) -> None:
    runtime_request, _ = _single_agent_runtime(tmp_path, mode="allow")

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
                execution_engine="single_agent",
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

    def single_agent_provider(self) -> object:
        outcomes = list(self.outcomes)
        name = self.name

        class _Provider:
            def __init__(self) -> None:
                self.name = name

            def propose_turn(self, request: object) -> object:
                _ = request
                if not outcomes:
                    return importlib.import_module(
                        "voidcode.runtime.single_agent_provider"
                    ).SingleAgentTurnResult(output="done")
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        return _Provider()


def test_single_agent_runtime_falls_back_to_next_provider_target(tmp_path: Path) -> None:
    runtime_request, _ = _load_runtime_types()
    permission_module = importlib.import_module("voidcode.runtime.permission")
    config_module = importlib.import_module("voidcode.runtime.config")
    model_provider_module = importlib.import_module("voidcode.provider.registry")
    single_agent_provider_module = importlib.import_module("voidcode.runtime.single_agent_provider")
    service_module = importlib.import_module("voidcode.runtime.service")

    runtime = cast(
        RuntimeRunner,
        service_module.VoidCodeRuntime(
            workspace=tmp_path,
            config=config_module.RuntimeConfig(
                approval_mode="allow",
                execution_engine="single_agent",
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
                            single_agent_provider_module.ProviderExecutionError(
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
                            single_agent_provider_module.SingleAgentTurnResult(
                                output="fallback ok"
                            ),
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert allowed.events[6].payload["decision"] == "allow"
    assert allowed.output == "approved write"
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved write"


def test_runtime_tool_request_created_supports_non_path_tool_arguments(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")

    command = _cwd_command()
    prompt = f"run {command}"
    result = runtime.run(runtime_request(prompt=prompt, session_id="command-session"))

    assert result.events[1].event_type == "runtime.skills_loaded"
    assert result.events[1].payload == {"skills": []}
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert allowed.events[6].payload["decision"] == "allow"
    assert allowed.output == f"{tmp_path.resolve()}\n"
    assert allowed.events[7].payload["command"] == command
    assert allowed.events[7].payload["exit_code"] == 0


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
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert resumed.output == f"{tmp_path.resolve()}\n"
    assert resumed.events[12].payload["command"] == command
    assert resumed.events[12].payload["exit_code"] == 0


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
    assert result.events[9].payload == {
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
    assert "EVENT runtime.request_received" in nested_output_path.read_text(encoding="utf-8")
    assert "runtime.tool_hook_pre" not in nested_output_path.read_text(encoding="utf-8")
    assert "runtime.tool_hook_post" not in nested_output_path.read_text(encoding="utf-8")
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
            _ = runtime.run(
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

    assert [event.event_type for event in replay.events][-3:] == [
        "runtime.approval_resolved",
        "runtime.tool_hook_pre",
        "runtime.failed",
    ]
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
            _ = runtime.run(runtime_request(prompt="write danger.txt broken", session_id="s1"))

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
    assert resumed.events[-1].event_type == "runtime.failed"
    assert resumed.events[-1].payload == {"error": "boom"}


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


def test_runtime_executes_read_only_slice_and_emits_events(tmp_path: Path) -> None:
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert result.events[1].payload == {"skills": []}
    assert result.session.status == "completed"
    assert result.output == "alpha\nbeta\n"


def test_single_agent_runtime_executes_read_path_and_persists_config(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\nbeta\n", encoding="utf-8")
    runtime_request, runtime = _single_agent_runtime(tmp_path, mode="allow")

    result = runtime.run(runtime_request(prompt="read sample.txt", session_id="single-agent-read"))
    replay = runtime.resume("single-agent-read")

    assert result.session.status == "completed"
    assert result.output == "alpha\nbeta\n"
    assert result.session.metadata["runtime_config"] == {
        "approval_mode": "allow",
        "execution_engine": "single_agent",
        "max_steps": 4,
        "lsp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "mcp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "model": "opencode/gpt-5.4",
        "provider_fallback": None,
        "plan": None,
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
    assert result.session.metadata["runtime_state"] == {
        "acp": {
            "available": False,
            "configured_enabled": False,
            "last_error": None,
            "mode": "disabled",
            "status": "disconnected",
        }
    }
    assert result.events[3].payload["mode"] == "single_agent"
    assert replay.output == result.output


def test_single_agent_runtime_requests_and_resumes_write_approval(tmp_path: Path) -> None:
    runtime_request, runtime = _single_agent_runtime(tmp_path, mode="ask")

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
    assert waiting.events[3].payload["mode"] == "single_agent"
    assert waiting.events[-1].event_type == "runtime.approval_requested"
    assert resumed.session.status == "completed"
    assert resumed.output == "approved later"
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
        if current.status in ("completed", "failed", "cancelled"):
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
        if current.status in ("completed", "failed", "cancelled"):
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

    assert cancelled.status == "failed"
    assert cancelled.error == "background task interrupted before completion"
    assert cancelled.cancel_requested_at is None


def test_runtime_executes_grep_read_only_slice_and_emits_events(tmp_path: Path) -> None:
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert result.events[4].payload == {
        "tool": "grep",
        "arguments": {"pattern": "alpha", "path": "sample.txt"},
        "path": "sample.txt",
    }
    assert result.events[7].payload == {
        "tool": "grep",
        "status": "ok",
        "content": "Found 2 match(es) for 'alpha' in sample.txt\n1: alpha\n2: beta alpha",
        "error": None,
        "path": "sample.txt",
        "pattern": "alpha",
        "match_count": 2,
        "matches": [
            {"line": 1, "text": "alpha", "columns": [1]},
            {"line": 2, "text": "beta alpha", "columns": [6]},
        ],
    }
    assert result.session.status == "completed"
    assert result.output == "Found 2 match(es) for 'alpha' in sample.txt\n1: alpha\n2: beta alpha"


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
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert resumed.output == "approved later"
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved later"


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
    assert [event.sequence for event in resumed.events] == list(range(1, 16))
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]
    assert [event.sequence for event in waiting.events] == list(range(1, 14))
    assert waiting.events[10].payload == {
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert [event.sequence for event in resumed.events] == list(range(1, 28))
    assert resumed.output == "Found 1 match(es) for 'copied' in copied.txt\n1: copied marker"
    assert replay.output == resumed.output
    assert [event.sequence for event in replay.events] == list(range(1, 28))
    assert [(event.sequence, event.event_type, event.payload) for event in replay.events] == [
        (event.sequence, event.event_type, event.payload) for event in resumed.events
    ]
    assert resumed.events[24].payload == {
        "tool": "grep",
        "status": "ok",
        "content": "Found 1 match(es) for 'copied' in copied.txt\n1: copied marker",
        "error": None,
        "path": "copied.txt",
        "pattern": "copied",
        "match_count": 1,
        "matches": [{"line": 1, "text": "copied marker", "columns": [1]}],
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert [event.sequence for event in denied.events] == list(range(1, 20))
    assert denied.events[-1].payload == {"error": "permission denied for tool: write_file"}
    assert denied.output is None
    assert replay.output is None
    assert [(event.sequence, event.event_type, event.payload) for event in replay.events] == [
        (event.sequence, event.event_type, event.payload) for event in denied.events
    ]
    assert [event.event_type for event in denied.events].count("graph.tool_request_created") == 3
    assert "grep" not in [
        cast(str, event.payload.get("tool"))
        for event in denied.events
        if event.event_type == "graph.tool_request_created"
    ]
    assert [summary.session.id for summary in sessions] == ["deny-loop-session"]
    assert sessions[0].status == "failed"
    assert sessions[0].updated_at == 2
    assert (tmp_path / "copied.txt").exists() is False


def test_runtime_migrates_legacy_session_schema_for_pending_approval(tmp_path: Path) -> None:
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

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt legacy approval", session_id="legacy-session")
    )

    assert waiting.session.status == "waiting"

    check = sqlite3.connect(database_path)
    try:
        session_rows = cast(
            list[tuple[object, ...]], check.execute("PRAGMA table_info(sessions)").fetchall()
        )
        background_task_rows = cast(
            list[tuple[object, ...]],
            check.execute("PRAGMA table_info(background_tasks)").fetchall(),
        )
        delivery_tables = cast(
            list[tuple[object, ...]],
            check.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'session_event_deliveries'"
            ).fetchall(),
        )
        columns = [cast(str, row[1]) for row in session_rows]
        background_task_columns = [cast(str, row[1]) for row in background_task_rows]
        user_version = cast(int, check.execute("PRAGMA user_version").fetchone()[0])
    finally:
        check.close()

    assert "parent_session_id" in columns
    assert "pending_approval_json" in columns
    assert "resume_checkpoint_json" in columns
    assert "request_parent_session_id" in background_task_columns
    assert delivery_tables == [("session_event_deliveries",)]
    assert user_version == 6


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

    assert replay.session.metadata["runtime_config"] == {
        "approval_mode": "allow",
        "execution_engine": "deterministic",
        "max_steps": 4,
        "tool_timeout_seconds": None,
        "lsp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "mcp": {"configured_enabled": False, "mode": "disabled", "servers": []},
        "model": "session/model",
        "provider_fallback": None,
        "plan": None,
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
    assert replay.session.metadata["runtime_state"] == {
        "acp": {
            "available": False,
            "configured_enabled": False,
            "last_error": None,
            "mode": "disabled",
            "status": "disconnected",
        }
    }


def test_runtime_resume_accepts_legacy_sessions_without_runtime_config_metadata(
    tmp_path: Path,
) -> None:
    _ = (tmp_path / "sample.txt").write_text("legacy config\n", encoding="utf-8")
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")
    response = runtime.run(
        runtime_request(prompt="read sample.txt", session_id="legacy-runtime-config")
    )

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = cast(
            tuple[str],
            connection.execute(
                "SELECT metadata_json FROM sessions WHERE session_id = ?",
                ("legacy-runtime-config",),
            ).fetchone(),
        )
        metadata = cast(dict[str, object], json.loads(row[0]))
        metadata.pop("runtime_config", None)
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata, sort_keys=True), "legacy-runtime-config"),
        )
        connection.commit()
    finally:
        connection.close()

    replay = runtime.resume("legacy-runtime-config")

    assert replay.session.status == response.session.status
    assert replay.output == response.output
    assert replay.session.metadata == {
        "workspace": str(tmp_path),
        "runtime_state": {
            "acp": {
                "available": False,
                "configured_enabled": False,
                "last_error": None,
                "mode": "disabled",
                "status": "disconnected",
            }
        },
        "context_window": {
            "compacted": False,
            "compaction_reason": None,
            "original_tool_result_count": 1,
            "retained_tool_result_count": 1,
            "max_tool_result_count": 4,
        },
    }


def test_runtime_resume_repairs_legacy_non_dict_runtime_state_metadata(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    waiting = runtime.run(
        runtime_request(prompt="write danger.txt approved later", session_id="legacy-runtime-state")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    database_path = tmp_path / ".voidcode" / "sessions.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        row = cast(
            tuple[str],
            connection.execute(
                "SELECT metadata_json FROM sessions WHERE session_id = ?",
                ("legacy-runtime-state",),
            ).fetchone(),
        )
        metadata = cast(dict[str, object], json.loads(row[0]))
        metadata["runtime_state"] = "broken"
        _ = connection.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata, sort_keys=True), "legacy-runtime-state"),
        )
        connection.commit()
    finally:
        connection.close()

    replay = runtime.resume(
        "legacy-runtime-state",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert replay.session.status == "completed"
    assert replay.output == "approved later"
    assert replay.session.metadata["runtime_state"] == {
        "acp": {
            "available": False,
            "configured_enabled": False,
            "last_error": None,
            "mode": "disabled",
            "status": "disconnected",
        }
    }


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
        failed = resumed_runtime.resume(
            "approval-session",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )

    assert failed.session.status == "failed"
    assert failed.events[-2].event_type == "runtime.approval_resolved"
    assert failed.events[-1].event_type == "runtime.failed"
    assert failed.events[-1].payload == {"error": "resume boom"}

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
    with pytest.raises(ValueError, match="no pending approval"):
        _ = replay_runtime.resume(
            "approval-session",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )


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


def test_cli_run_command_prints_events_and_file_contents(tmp_path: Path) -> None:
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
    assert "EVENT runtime.request_received" in result.stdout
    assert "EVENT runtime.tool_completed" in result.stdout
    assert "RESULT" in result.stdout
    assert "slice proof" in result.stdout


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
    pre_finalization_chunks = chunks[:8]
    final_chunks = chunks[8:]

    assert [chunk.event.event_type for chunk in event_chunks if chunk.event is not None] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
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
    ]
    assert [chunk.session.status for chunk in final_chunks] == [
        "completed",
        "completed",
        "completed",
    ]
    assert [chunk.output for chunk in output_chunks] == ["stream proof\n"]
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

        first_four_chunks = [next(stream) for _ in range(7)]

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
            "delayed stream\n"
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

        first_four_chunks = [next(stream) for _ in range(7)]
        failed_chunk = next(stream)

        with pytest.raises(ValueError, match="boom from tool"):
            _ = next(stream)

    assert [chunk.event.event_type for chunk in first_four_chunks if chunk.event is not None] == [
        "runtime.request_received",
        "runtime.skills_loaded",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
    ]
    assert all(chunk.session.status == "running" for chunk in first_four_chunks)
    assert failed_chunk.kind == "event"
    assert failed_chunk.event is not None
    assert failed_chunk.event.event_type == "runtime.failed"
    assert failed_chunk.event.payload == {"error": "boom from tool"}
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
            for _ in range(5):
                first_chunks.append(next(stream))
            first_chunks_ready.set()

        first_worker = threading.Thread(target=_consume_first_chunks)
        first_worker.start()
        first_worker.join(timeout=1)

        assert first_worker.is_alive() is False
        assert first_chunks_ready.is_set() is True
        assert tool_started.is_set() is False
        assert [chunk.event.event_type for chunk in first_chunks if chunk.event is not None] == [
            "graph.loop_step",
            "graph.model_turn",
            "graph.tool_request_created",
            "runtime.tool_lookup_succeeded",
            "runtime.approval_resolved",
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
            "resumed later"
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
        "runtime.tool_completed",
        "graph.loop_step",
        "graph.response_ready",
    ]
    assert [chunk.session.status for chunk in completed_chunks[:8]] == [
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
        "running",
    ]
    assert [chunk.session.status for chunk in completed_chunks[8:]] == [
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

    assert [chunk.event.event_type for chunk in failed_chunks[-7:] if chunk.event is not None] == [
        "runtime.approval_requested",
        "graph.loop_step",
        "graph.model_turn",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert [chunk.session.status for chunk in failed_chunks[-7:]] == [
        "waiting",
        "running",
        "running",
        "running",
        "running",
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
