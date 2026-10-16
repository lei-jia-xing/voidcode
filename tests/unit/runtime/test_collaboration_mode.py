from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn, TurnPlan, TurnRequest
from voidcode.runtime.active_session import ACTIVE_SESSION_REGISTRY
from voidcode.runtime.contracts import RuntimeRequest, RuntimeRequestError
from voidcode.runtime.permissions.policy import (
    READ_ONLY_DENIAL_REASON,
    runtime_effective_read_only_from_metadata,
)
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.session_metadata_helpers import collaboration_mode_from_metadata
from voidcode.tools.contracts import ToolCall


class _StepProducer:
    def __init__(self, steps: Sequence[tuple[str, Mapping[str, object]]]) -> None:
        self.steps = steps

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> TurnPlan:
        if request.run_step <= len(self.steps):
            tool_name, arguments = self.steps[request.run_step - 1]
            return ToolTurn(
                facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),),
                calls=(ToolCall(tool_name=tool_name, arguments=dict(arguments)),),
            )
        return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output="done")


def test_collaboration_mode_persistence_and_reopen(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("initial content\n")
    h_initial = hashlib.sha256(b"initial content\n").hexdigest()

    # 1. Writable initial run
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_StepProducer([("read", {"path": "file.txt"})])) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="initial read"))
        session_id = resp.session.session.id
        assert resp.session.status == "completed"
        assert not resp.session.metadata.get("read_only")
        assert collaboration_mode_from_metadata(resp.session.metadata) == "default"

        # 2. Enter plan mode
        res = runtime.set_collaboration_mode(session_id=session_id, mode="plan")
        assert res["collaboration_mode"] == "plan"
        assert res["previous_mode"] == "default"

        loaded = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        assert loaded.session.metadata.get("collaboration_mode") == "plan"
        assert not loaded.session.metadata.get("read_only")
        assert runtime_effective_read_only_from_metadata(loaded.session.metadata) is True

        mode_events = [e for e in loaded.events if e.event_type == "runtime.collaboration_mode_changed"]
        assert len(mode_events) == 1
        assert mode_events[0].payload == {"mode": "plan", "previous_mode": "default"}

        assembled = runtime.assemble_provider_context(
            prompt="planning prompt",
            tool_results=(),
            session_metadata=dict(loaded.session.metadata),
        )
        assert any((s.metadata or {}).get("source") == "runtime_collaboration_mode" for s in assembled.segments)

    # 3. Reopen in fresh runtime: plan mode persists and blocks mutating write
    edit_step = [
        ("read", {"path": "file.txt"}),
        ("edit", {"path": "file.txt", "oldString": "initial", "newString": "updated", "expectedHash": h_initial}),
    ]
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_StepProducer(edit_step)) as reopened_runtime:
        reopened = reopened_runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        assert reopened.session.metadata.get("collaboration_mode") == "plan"
        assert runtime_effective_read_only_from_metadata(reopened.session.metadata) is True

        try:
            reopened_runtime.run(RuntimeRequest(session_id=session_id, prompt="attempt edit in plan"))
            raise AssertionError("mutation should be blocked in plan mode")
        except ValueError as exc:
            assert READ_ONLY_DENIAL_REASON in str(exc)
        assert target.read_text() == "initial content\n"

        # 4. Turn plan mode off: write restores and modifies disk
        off_res = reopened_runtime.set_collaboration_mode(session_id=session_id, mode="default")
        assert off_res["collaboration_mode"] == "default"
        assert off_res["previous_mode"] == "plan"

        loaded_off = reopened_runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        assert loaded_off.session.metadata.get("collaboration_mode") == "default"
        assert not loaded_off.session.metadata.get("read_only")
        assert runtime_effective_read_only_from_metadata(loaded_off.session.metadata) is False

        reopened_runtime._turn_producer_override = _StepProducer(edit_step)
        resp_after = reopened_runtime.run(RuntimeRequest(session_id=session_id, prompt="execute edit after plan off"))
        assert resp_after.session.status == "completed"
        assert target.read_text() == "updated content\n"


def test_collaboration_mode_frozen_readonly_and_child_inheritance(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("data\n")
    h = hashlib.sha256(b"data\n").hexdigest()
    edit_step = [
        ("read", {"path": "file.txt"}),
        ("edit", {"path": "file.txt", "oldString": "data", "newString": "tampered", "expectedHash": h}),
    ]

    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_StepProducer([("read", {"path": "file.txt"})])) as runtime:
        # Frozen readonly session
        ro_resp = runtime.run(RuntimeRequest(prompt="ro start", metadata={"read_only": True}))
        ro_session_id = ro_resp.session.session.id
        assert ro_resp.session.metadata.get("read_only") is True

        # Enter plan mode, then exit plan mode: base read_only remains True
        runtime.set_collaboration_mode(session_id=ro_session_id, mode="plan")
        runtime.set_collaboration_mode(session_id=ro_session_id, mode="default")
        ro_default = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=ro_session_id)
        assert ro_default.session.metadata.get("read_only") is True
        assert runtime_effective_read_only_from_metadata(ro_default.session.metadata) is True

        # Actual write attempt remains denied and disk is unchanged
        runtime._turn_producer_override = _StepProducer(edit_step)
        try:
            runtime.run(RuntimeRequest(session_id=ro_session_id, prompt="attempt write in readonly"))
            raise AssertionError("write must remain blocked on frozen readonly session")
        except ValueError as exc:
            assert READ_ONLY_DENIAL_REASON in str(exc)
        assert target.read_text() == "data\n"

        # Child session spawned under parent in plan mode inherits restriction
        runtime._turn_producer_override = _StepProducer([("read", {"path": "file.txt"})])
        writable_resp = runtime.run(RuntimeRequest(session_id="parent-session", prompt="writable start"))
        parent_id = writable_resp.session.session.id
        runtime.set_collaboration_mode(session_id=parent_id, mode="plan")

        runtime._turn_producer_override = _StepProducer(edit_step)
        try:
            runtime.run(RuntimeRequest(session_id="child-session", parent_session_id=parent_id, prompt="child attempting edit"))
            raise AssertionError("child of plan session must be denied write")
        except ValueError as exc:
            assert READ_ONLY_DENIAL_REASON in str(exc)
        assert target.read_text() == "data\n"


def test_collaboration_mode_concurrency_and_cas_integrity(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("data\n")
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_StepProducer([("read", {"path": "file.txt"})])) as runtime:
        resp = runtime.run(RuntimeRequest(prompt="initial"))
        session_id = resp.session.session.id

        # 1. Active session running rejection
        ACTIVE_SESSION_REGISTRY.register(workspace=tmp_path, session_id=session_id, run_id="active-run", metadata={})
        try:
            runtime.set_collaboration_mode(session_id=session_id, mode="plan")
            raise AssertionError("should reject active running session")
        except ValueError as exc:
            assert "cannot change collaboration mode while a session is running" in str(exc)
        finally:
            ACTIVE_SESSION_REGISTRY.unregister(workspace=tmp_path, session_id=session_id, run_id="active-run")

        # Snapshot before CAS attempt
        baseline = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        baseline_event_count = len(baseline.events)
        baseline_metadata = dict(baseline.session.metadata)

        # 2. Stale expected sequence CAS rejection: assert no partial writes
        try:
            runtime._repositories.sessions.append_collaboration_mode_change(
                workspace=tmp_path,
                session_id=session_id,
                metadata={"collaboration_mode": "plan"},
                mode="plan",
                previous_mode="default",
                expected_sequence=9999,
                expected_status="completed",
            )
            raise AssertionError("should reject stale CAS sequence")
        except (RuntimeError, ValueError) as exc:
            assert "session changed while updating collaboration mode" in str(exc)

        after_stale = runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id)
        assert len(after_stale.events) == baseline_event_count
        assert dict(after_stale.session.metadata) == baseline_metadata

        # 3. Malformed/corrupted mode token fails visibly
        try:
            runtime.set_collaboration_mode(session_id=session_id, mode="invalid_mode")
            raise AssertionError("should reject invalid mode")
        except ValueError as exc:
            assert "invalid collaboration mode" in str(exc)

        # 4. Same-mode no-op returns unchanged without appending new event
        noop_res = runtime.set_collaboration_mode(session_id=session_id, mode="default")
        assert noop_res.get("unchanged") is True
        assert len(runtime._repositories.sessions.load_session(workspace=tmp_path, session_id=session_id).events) == baseline_event_count

        # 5. Prompt command rejection
        try:
            runtime.run(RuntimeRequest(prompt="/plan"))
            raise AssertionError("should reject slash prompt")
        except RuntimeRequestError as exc:
            assert "native command /plan requires an active session" in str(exc)
