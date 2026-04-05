"""Integration tests for the deterministic read-only slice."""

from __future__ import annotations

import importlib
import os
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


class SessionRefLike(Protocol):
    id: str


class StoredSessionSummaryLike(Protocol):
    session: SessionRefLike


class RuntimeResponseLike(Protocol):
    events: tuple[EventLike, ...]
    output: str | None
    session: SessionLike


class RuntimeRequestLike(Protocol):
    prompt: str
    metadata: dict[str, object]


class RuntimeRequestFactory(Protocol):
    def __call__(self, *, prompt: str, session_id: str | None = None) -> RuntimeRequestLike: ...


class RuntimeRunner(Protocol):
    def run(self, request: RuntimeRequestLike) -> RuntimeResponseLike: ...

    def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]: ...

    def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]: ...

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: str | None = None,
    ) -> RuntimeResponseLike: ...


class RuntimeFactory(Protocol):
    def __call__(
        self,
        *,
        workspace: Path,
        tool_registry: object | None = None,
        graph: object | None = None,
        permission_policy: object | None = None,
        session_store: object | None = None,
    ) -> RuntimeRunner: ...


class ToolCallFactory(Protocol):
    def __call__(self, *, tool_name: str, arguments: dict[str, object]) -> object: ...


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


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)
    runtime_class = cast(RuntimeFactory, service_module.VoidCodeRuntime)
    return runtime_request, runtime_class


@dataclass(frozen=True, slots=True)
class _WritePlan:
    tool_call: object


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


def test_runtime_allows_non_read_only_tool_when_policy_is_allow(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")

    allowed = runtime.run(
        runtime_request(prompt="write danger.txt approved write", session_id="allow-session")
    )

    assert allowed.session.status == "completed"
    assert [event.event_type for event in allowed.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert allowed.events[3].payload["decision"] == "allow"
    assert allowed.output == "approved write"
    assert (tmp_path / "danger.txt").read_text(encoding="utf-8") == "approved write"


def test_runtime_tool_request_created_supports_non_path_tool_arguments(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")

    result = runtime.run(runtime_request(prompt="run pwd", session_id="command-session"))

    tool_request_event = result.events[1]
    assert tool_request_event.event_type == "graph.tool_request_created"
    assert tool_request_event.payload == {
        "tool": "shell_exec",
        "arguments": {"command": "pwd"},
    }


def test_runtime_allows_shell_exec_tool_when_policy_is_allow(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="allow")

    allowed = runtime.run(runtime_request(prompt="run pwd", session_id="shell-allow-session"))

    assert allowed.session.status == "completed"
    assert [event.event_type for event in allowed.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert allowed.events[3].payload["decision"] == "allow"
    assert allowed.output == f"{tmp_path.resolve()}\n"
    assert allowed.events[4].payload["command"] == "pwd"
    assert allowed.events[4].payload["exit_code"] == 0


def test_runtime_requests_and_resumes_shell_exec_approval(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(runtime_request(prompt="run pwd", session_id="shell-approval-session"))

    assert waiting.session.status == "waiting"
    assert [event.event_type for event in waiting.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
    ]
    assert waiting.events[-1].payload["tool"] == "shell_exec"
    assert waiting.events[-1].payload["arguments"] == {"command": "pwd"}
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    resumed = runtime.resume(
        "shell-approval-session",
        approval_request_id=approval_request_id,
        approval_decision="allow",
    )

    assert resumed.session.status == "completed"
    assert [event.event_type for event in resumed.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert resumed.output == f"{tmp_path.resolve()}\n"
    assert resumed.events[5].payload["command"] == "pwd"
    assert resumed.events[5].payload["exit_code"] == 0


def test_runtime_denies_shell_exec_tool_when_policy_is_deny(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="deny")

    denied = runtime.run(runtime_request(prompt="run pwd", session_id="shell-deny-session"))

    assert denied.session.status == "failed"
    assert [event.event_type for event in denied.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert denied.events[3].payload["decision"] == "deny"
    assert denied.output is None


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
        def plan(self, _request: object) -> object:
            return _WritePlan(
                tool_call=cast(
                    ToolCallFactory, importlib.import_module("voidcode.tools.contracts").ToolCall
                )(
                    tool_name="write_file",
                    arguments={"path": "danger.txt", "content": "broken finalize"},
                )
            )

        def finalize(self, request: object, tool_result: object, *, session: object) -> object:
            _ = request
            _ = tool_result
            _ = session
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
        def plan(self, _request: object) -> object:
            raise RuntimeError("plan boom")

        def finalize(self, request: object, tool_result: object, *, session: object) -> object:
            _ = request
            _ = tool_result
            _ = session
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
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_resolved",
        "runtime.failed",
    ]
    assert denied.events[3].payload["decision"] == "deny"
    assert denied.output is None
    assert (tmp_path / "danger.txt").exists() is False


def test_runtime_executes_read_only_slice_and_emits_events(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\nbeta\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    runtime = runtime_class(workspace=tmp_path)
    result = runtime.run(runtime_request(prompt="read sample.txt"))

    assert [event.event_type for event in result.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert result.session.status == "completed"
    assert result.output == "alpha\nbeta\n"


def test_runtime_allows_non_read_only_tool_after_explicit_resume_approval(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt approved later", session_id="approval-session")
    )

    assert waiting.session.status == "waiting"
    assert [event.event_type for event in waiting.events] == [
        "runtime.request_received",
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
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.approval_requested",
        "runtime.approval_resolved",
        "runtime.tool_completed",
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
    assert [event.sequence for event in resumed.events] == [1, 2, 3, 4, 5, 6, 7]
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
        rows = cast(
            list[tuple[object, ...]], check.execute("PRAGMA table_info(sessions)").fetchall()
        )
        columns = [cast(str, row[1]) for row in rows]
        user_version = cast(int, check.execute("PRAGMA user_version").fetchone()[0])
    finally:
        check.close()

    assert "pending_approval_json" in columns
    assert user_version == 2


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


def test_runtime_marks_resumed_finalize_failure_and_clears_pending_request(tmp_path: Path) -> None:
    runtime_request, runtime = _approval_runtime(tmp_path, mode="ask")
    permission_module = importlib.import_module("voidcode.runtime.permission")
    policy = cast(Callable[..., object], permission_module.PermissionPolicy)(mode="ask")

    waiting = runtime.run(
        runtime_request(prompt="write danger.txt finalize failure", session_id="approval-session")
    )
    approval_request_id = cast(str, waiting.events[-1].payload["request_id"])

    class FailingFinalizeGraph:
        def plan(self, _request: object) -> object:
            return _WritePlan(
                tool_call=cast(
                    ToolCallFactory, importlib.import_module("voidcode.tools.contracts").ToolCall
                )(
                    tool_name="write_file",
                    arguments={"path": "danger.txt", "content": "finalize failure"},
                )
            )

        def finalize(self, request: object, tool_result: object, *, session: object) -> object:
            _ = request
            _ = tool_result
            _ = session
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
    assert failed.events[-2].event_type == "runtime.tool_completed"
    assert failed.events[-1].event_type == "runtime.failed"
    assert failed.events[-1].payload == {"error": "finalize boom"}

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
    with pytest.raises(ValueError, match="no pending approval"):
        _ = replay_runtime.resume(
            "approval-session",
            approval_request_id=approval_request_id,
            approval_decision="allow",
        )


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
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
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
    pre_finalization_chunks = chunks[:5]
    final_chunks = chunks[5:]

    assert [chunk.event.event_type for chunk in event_chunks if chunk.event is not None] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert [chunk.session.status for chunk in pre_finalization_chunks] == [
        "running",
        "running",
        "running",
        "running",
        "running",
    ]
    assert [chunk.session.status for chunk in final_chunks] == ["completed", "completed"]
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

        first_four_chunks = [next(stream) for _ in range(4)]

        assert [
            chunk.event.event_type for chunk in first_four_chunks if chunk.event is not None
        ] == [
            "runtime.request_received",
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
        ] == ["graph.response_ready"]
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

        first_four_chunks = [next(stream) for _ in range(4)]
        failed_chunk = next(stream)

        with pytest.raises(ValueError, match="boom from tool"):
            _ = next(stream)

    assert [chunk.event.event_type for chunk in first_four_chunks if chunk.event is not None] == [
        "runtime.request_received",
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
