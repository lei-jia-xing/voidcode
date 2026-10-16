from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.turns import FinalTurn, ModelTurnFact, TurnRequest, TurnSession
from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY
from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.contracts import (
    RuntimeRequest,
    RuntimeRequestError,
    SessionBusyConflictError,
)
from voidcode.runtime.service import VoidCodeRuntime


class _FinishingTurnProducer:
    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: TurnSession) -> FinalTurn:
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        return FinalTurn(facts=(fact,), output="done")


def _seed_session(
    workspace: Path,
    session_id: str,
    *,
    prompt: str = "seed prompt",
    title: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    with VoidCodeRuntime(workspace=workspace, turn_producer=_FinishingTurnProducer()) as runtime:
        runtime.run(RuntimeRequest(prompt=prompt, session_id=session_id, metadata=metadata or {}))
        if title:
            runtime.rename_session(session_id=session_id, title=title)


def test_runtime_create_session_with_and_without_title(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        session1 = runtime.create_session()
        assert session1.session.id.startswith("session-")
        assert session1.title is None
        assert session1.turn == 0

        session2 = runtime.create_session(title="My Feature Session")
        assert session2.session.id.startswith("session-")
        assert session2.title == "My Feature Session"

        sessions = runtime.list_sessions()
        ids = {s.session.id for s in sessions}
        assert session1.session.id in ids
        assert session2.session.id in ids
    finally:
        runtime.__exit__(None, None, None)


def test_runtime_rename_session_persists_and_emits_event(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        session = runtime.create_session()
        renamed = runtime.rename_session(session_id=session.session.id, title="Refactored Code")
        assert renamed.title == "Refactored Code"

        stored = next(s for s in runtime.list_sessions() if s.session.id == session.session.id)
        assert stored.title == "Refactored Code"

        # Check emitted event in storage
        events = runtime._repositories.sessions.load_session(
            workspace=workspace,
            session_id=session.session.id,
        ).events
        rename_events = [e for e in events if e.event_type == "runtime.session_renamed"]
        assert len(rename_events) == 1
        assert rename_events[0].payload == {"title": "Refactored Code"}
    finally:
        runtime.__exit__(None, None, None)


def test_runtime_rename_empty_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        session = runtime.create_session()
        with pytest.raises(RuntimeRequestError, match="title must be a non-empty string"):
            runtime.rename_session(session_id=session.session.id, title="   ")
        with pytest.raises(RuntimeRequestError, match="title must be a non-empty string"):
            runtime.rename_session(session_id=session.session.id, title="")
    finally:
        runtime.__exit__(None, None, None)


def test_runtime_fork_carries_settings_and_renames(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = RuntimeConfig(model="deepseek/deepseek-chat", reasoning_effort="high")
    with VoidCodeRuntime(workspace=workspace, config=config, turn_producer=_FinishingTurnProducer()) as seed_rt:
        seed_rt.run(RuntimeRequest(prompt="Initial prompt", session_id="source-session"))

    runtime = VoidCodeRuntime(workspace=workspace, config=config)
    try:
        forked = runtime.fork_session(session_id="source-session")
        assert forked.session.id != "source-session"
        assert forked.forked_from_session_id == "source-session"

        # Check carried settings in forked session metadata
        loaded = runtime._repositories.sessions.load_session(
            workspace=workspace,
            session_id=forked.session.id,
        )
        execution_meta = loaded.session.metadata["agent_capability_snapshot"]["execution"]
        assert execution_meta["model"] == "deepseek/deepseek-chat"
        assert execution_meta["reasoning_effort"] == "high"

        # Optional rename
        renamed_fork = runtime.rename_session(session_id=forked.session.id, title="Forked Feature")
        assert renamed_fork.title == "Forked Feature"
    finally:
        runtime.__exit__(None, None, None)


def test_runtime_busy_gate_matrix(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = VoidCodeRuntime(workspace=workspace)
    try:
        _seed_session(workspace, "active-session", prompt="Do work")
        run_id = "test-run-1"
        ACTIVE_SESSION_REGISTRY.register(
            workspace=workspace,
            session_id="active-session",
            run_id=run_id,
            metadata={"running": True},
        )
        try:
            assert ACTIVE_SESSION_REGISTRY.active_run_count(workspace=workspace, session_id="active-session") == 1

            # 1. fork_session BLOCKED during task
            with pytest.raises(SessionBusyConflictError, match="cannot fork a session while it is running"):
                runtime.fork_session(session_id="active-session")

            # 2. rename_session ALLOWED during task (runs concurrently)
            renamed = runtime.rename_session(session_id="active-session", title="Renamed While Running")
            assert renamed.title == "Renamed While Running"
        finally:
            ACTIVE_SESSION_REGISTRY.unregister(
                workspace=workspace,
                session_id="active-session",
                run_id=run_id,
            )
    finally:
        runtime.__exit__(None, None, None)
