from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from voidcode.runtime.composition import (
    ActiveComposition,
    CapabilityBinding,
    ExecutionPlan,
    FrozenComposition,
    PhaseBinding,
    PhaseCompleted,
    PhaseContext,
    PhaseDeclaration,
    PhaseFailed,
    PhaseRecord,
    _digest,
)
from voidcode.tools.contracts import ToolCall


def _make_active(phases: list[PhaseDeclaration], phase_funcs: dict[str, tuple[Any, Any, Any, Any]]) -> ActiveComposition:
    binding_payload = {
        "schema_version": 1,
        "packages": [],
        "components": [
            {
                "key": "test:tool:phase_tool",
                "distribution": "test",
                "slot": "tool",
                "name": "phase_tool",
                "configuration": {},
                "provenance": ("test",),
            }
        ],
        "intent": {},
    }
    binding_id = _digest(binding_payload)
    binding = CapabilityBinding(binding_id=binding_id, **binding_payload)

    sorted_phases = sorted(phases, key=lambda p: p.order)
    phase_bindings = [
        PhaseBinding(
            key=f"test:{phase.name}",
            component_key="test:tool:phase_tool",
            distribution="test",
            declaration=phase,
        )
        for phase in sorted_phases
    ]
    plan_payload = {
        "schema_version": 1,
        "binding_id": binding_id,
        "components": ("test:tool:phase_tool",),
        "phases": tuple(phase_bindings),
    }
    plan_id = _digest({**plan_payload, "phases": [pb.model_dump(mode="json") for pb in phase_bindings]})
    plan = ExecutionPlan(plan_id=plan_id, **plan_payload)
    frozen = FrozenComposition(binding=binding, plan=plan)
    frozen.verify()

    return ActiveComposition(
        frozen,
        values={"test:tool:phase_tool": {}},
        factories={"test:tool:phase_tool": lambda val, ctx: "tool_instance"},
        phase_functions=phase_funcs,
    )


def _context(tmp_path: Path, scope: str = "turn", scope_id: str = "turn-1") -> PhaseContext:
    return PhaseContext(
        workspace=tmp_path,
        session_id="test-session",
        run_id="run-1",
        scope=scope,  # type: ignore[arg-type]
        scope_id=scope_id,
    )


def test_declared_ordering_differing_from_lexical_declaration(tmp_path: Path) -> None:
    # Lexical order: Phase C (50), Phase A (10), Phase B (30)
    execution_order: list[str] = []

    def make_phase(name: str, order: int) -> PhaseDeclaration:
        return PhaseDeclaration(
            name=name,
            trigger="before_turn",
            slot="tool",
            component="phase_tool",
            callback="mod:cb",
            validate_input="mod:vi",
            snapshot="mod:snap",
            restore="mod:rest",
            scope="turn",
            order=order,
            version="1",
            failure="continue",
            replay="once",
        )

    phases = [make_phase("phase_c", 50), make_phase("phase_a", 10), make_phase("phase_b", 30)]

    def cb_factory(name: str):
        def cb(inp: Any, inst: Any, ctx: Any) -> str:
            execution_order.append(name)
            return name

        return cb

    phase_funcs = {
        f"test:{p.name}": (
            lambda val: val,
            cb_factory(p.name),
            lambda outcome: {"res": outcome.value if isinstance(outcome, PhaseCompleted) else str(outcome)},
            lambda payload, inst, ctx: payload["res"],
        )
        for p in phases
    }

    active = _make_active(phases, phase_funcs)
    records: dict[str, PhaseRecord] = {}

    outcomes = active.run_phase(
        "before_turn",
        "turn_input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )

    # Must execute by numeric order [A (10), B (30), C (50)], differing from lexical [C, A, B]
    assert execution_order == ["phase_a", "phase_b", "phase_c"]
    assert len(outcomes) == 3
    assert all(isinstance(o, PhaseCompleted) for o in outcomes)


def test_failure_policy_continue_records_failed_and_proceeds(tmp_path: Path) -> None:
    p1 = PhaseDeclaration(
        name="p1",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="continue",
        replay="once",
    )
    p2 = PhaseDeclaration(
        name="p2",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=20,
        version="1",
        failure="continue",
        replay="once",
    )

    def p1_cb(inp: Any, inst: Any, ctx: Any) -> Any:
        raise RuntimeError("p1 deliberate failure")

    def p2_cb(inp: Any, inst: Any, ctx: Any) -> str:
        return "p2 success"

    phase_funcs = {
        "test:p1": (
            lambda val: val,
            p1_cb,
            lambda outcome: {"err": outcome.error if isinstance(outcome, PhaseFailed) else "none"},
            lambda payload, inst, ctx: payload["err"],
        ),
        "test:p2": (
            lambda val: val,
            p2_cb,
            lambda outcome: {"ok": outcome.value if isinstance(outcome, PhaseCompleted) else "none"},
            lambda payload, inst, ctx: payload["ok"],
        ),
    }

    active = _make_active([p1, p2], phase_funcs)
    records: dict[str, PhaseRecord] = {}

    outcomes = active.run_phase(
        "before_turn",
        "turn_input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )

    assert len(outcomes) == 2
    assert isinstance(outcomes[0], PhaseFailed)
    assert outcomes[0].error == "p1 deliberate failure"
    assert isinstance(outcomes[1], PhaseCompleted)
    assert outcomes[1].value == "p2 success"

    # Both records were written with state='completed', p1 has failed=True, p2 has failed=False
    r1 = records["test:p1:turn:turn-1"]
    r2 = records["test:p2:turn:turn-1"]
    assert r1.state == "completed" and r1.failed is True
    assert r2.state == "completed" and r2.failed is False


def test_failure_policy_raise_records_failed_and_aborts_subsequent(tmp_path: Path) -> None:
    p1 = PhaseDeclaration(
        name="p1",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="raise",
        replay="once",
    )
    p2 = PhaseDeclaration(
        name="p2",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=20,
        version="1",
        failure="continue",
        replay="once",
    )

    p2_ran = False

    def p1_cb(inp: Any, inst: Any, ctx: Any) -> Any:
        raise RuntimeError("p1 deliberate abort")

    def p2_cb(inp: Any, inst: Any, ctx: Any) -> str:
        nonlocal p2_ran
        p2_ran = True
        return "p2 should not run"

    phase_funcs = {
        "test:p1": (
            lambda val: val,
            p1_cb,
            lambda outcome: {"err": outcome.error if isinstance(outcome, PhaseFailed) else "none"},
            lambda payload, inst, ctx: payload["err"],
        ),
        "test:p2": (
            lambda val: val,
            p2_cb,
            lambda outcome: {"ok": "ok"},
            lambda payload, inst, ctx: payload["ok"],
        ),
    }

    active = _make_active([p1, p2], phase_funcs)
    records: dict[str, PhaseRecord] = {}

    with pytest.raises(RuntimeError, match="p1 deliberate abort"):
        active.run_phase(
            "before_turn",
            "turn_input",
            scope="turn",
            scope_id="turn-1",
            context=_context(tmp_path),
            read_record=lambda k: records.get(k),
            write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
        )

    # p1 record was persisted with failed=True, state='completed' before exception was raised
    assert "test:p1:turn:turn-1" in records
    assert records["test:p1:turn:turn-1"].failed is True
    assert records["test:p1:turn:turn-1"].state == "completed"
    # p2 was NEVER executed
    assert not p2_ran
    assert "test:p2:turn:turn-1" not in records


def test_snapshot_records_latest_durable_state_and_order(tmp_path: Path) -> None:
    p = PhaseDeclaration(
        name="p_snap",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="continue",
        replay="once",
    )
    payload_captured: dict[str, Any] = {"version": 42, "state": "active_run"}

    phase_funcs = {
        "test:p_snap": (
            lambda val: val,
            lambda inp, inst, ctx: {"calculated": 100},
            lambda outcome: payload_captured,
            lambda payload, inst, ctx: payload,
        ),
    }

    active = _make_active([p], phase_funcs)
    records: dict[str, PhaseRecord] = {}

    active.run_phase(
        "before_turn",
        "turn_input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )

    rec = records["test:p_snap:turn:turn-1"]
    assert rec.payload == payload_captured
    assert rec.state == "completed"
    assert rec.failed is False


def test_replay_policy_once(tmp_path: Path) -> None:
    p = PhaseDeclaration(
        name="p_once",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="continue",
        replay="once",
    )
    cb_calls = 0

    def cb(inp: Any, inst: Any, ctx: Any) -> str:
        nonlocal cb_calls
        cb_calls += 1
        return "result"

    phase_funcs = {
        "test:p_once": (
            lambda val: val,
            cb,
            lambda outcome: {"res": "persisted"},
            lambda payload, inst, ctx: payload["res"],
        ),
    }

    active = _make_active([p], phase_funcs)
    # First execution: callback called, recorded as completed
    records: dict[str, PhaseRecord] = {}
    outcomes1 = active.run_phase(
        "before_turn",
        "input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )
    assert cb_calls == 1
    assert outcomes1[0].value == "result"

    # Second execution: replay='once' restores from record WITHOUT calling callback
    outcomes2 = active.run_phase(
        "before_turn",
        "input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )
    assert cb_calls == 1  # Not called again!
    assert outcomes2[0].value == "persisted"

    # Started record in-flight: refuses duplicate execution
    records["test:p_once:turn:turn-1"] = PhaseRecord.model_validate(
        {
            "phase_key": "test:p_once",
            "binding_id": active.frozen.binding.binding_id,
            "version": "1",
            "scope": "turn",
            "scope_id": "turn-1",
            "failed": False,
            "state": "started",
            "payload": {},
        }
    )
    with pytest.raises(ValueError, match="refuses duplicate execution"):
        active.run_phase(
            "before_turn",
            "input",
            scope="turn",
            scope_id="turn-1",
            context=_context(tmp_path),
            read_record=lambda k: records.get(k),
            write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
        )


def test_replay_policy_repeat(tmp_path: Path) -> None:
    p = PhaseDeclaration(
        name="p_repeat",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="continue",
        replay="repeat",
    )
    cb_calls = 0

    def cb(inp: Any, inst: Any, ctx: Any) -> str:
        nonlocal cb_calls
        cb_calls += 1
        return f"call_{cb_calls}"

    phase_funcs = {
        "test:p_repeat": (
            lambda val: val,
            cb,
            lambda outcome: {"res": outcome.value},
            lambda payload, inst, ctx: payload["res"],
        ),
    }

    active = _make_active([p], phase_funcs)
    records: dict[str, PhaseRecord] = {}
    active.run_phase(
        "before_turn",
        "input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )
    assert cb_calls == 1

    # Second execution: replay='repeat' executes callback again!
    active.run_phase(
        "before_turn",
        "input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )
    assert cb_calls == 2


def test_replay_policy_refuse(tmp_path: Path) -> None:
    p = PhaseDeclaration(
        name="p_refuse",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="continue",
        replay="refuse",
    )
    phase_funcs = {
        "test:p_refuse": (
            lambda val: val,
            lambda inp, inst, ctx: "ok",
            lambda outcome: {"res": "ok"},
            lambda payload, inst, ctx: payload["res"],
        ),
    }
    active = _make_active([p], phase_funcs)
    records: dict[str, PhaseRecord] = {}

    # Recovering without recorded phase raises
    with pytest.raises(ValueError, match="refuses unrecorded recovery execution"):
        active.run_phase(
            "before_turn",
            "input",
            scope="turn",
            scope_id="turn-1",
            context=_context(tmp_path),
            read_record=lambda k: records.get(k),
            write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
            recovering=True,
        )


def test_restore_only_requires_completed_durable_record(tmp_path: Path) -> None:
    p = PhaseDeclaration(
        name="p_rest",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="continue",
        replay="once",
    )
    phase_funcs = {
        "test:p_rest": (
            lambda val: val,
            lambda inp, inst, ctx: "callback_ran",
            lambda outcome: {"val": "restored_payload"},
            lambda payload, inst, ctx: payload["val"],
        ),
    }
    active = _make_active([p], phase_funcs)

    # Missing record raises
    records: dict[str, PhaseRecord] = {}
    with pytest.raises(ValueError, match="requires a completed durable phase record"):
        active.run_phase(
            "before_turn",
            "input",
            scope="turn",
            scope_id="turn-1",
            context=_context(tmp_path),
            read_record=lambda k: records.get(k),
            write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
            restore_only=True,
        )

    # Completed record restores payload
    records["test:p_rest:turn:turn-1"] = PhaseRecord.model_validate(
        {
            "phase_key": "test:p_rest",
            "binding_id": active.frozen.binding.binding_id,
            "version": "1",
            "scope": "turn",
            "scope_id": "turn-1",
            "failed": False,
            "state": "completed",
            "payload": {"val": "restored_payload"},
        }
    )
    outcomes = active.run_phase(
        "before_turn",
        "input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
        restore_only=True,
    )
    assert len(outcomes) == 1
    assert outcomes[0].value == "restored_payload"


def test_typed_input_consumed_from_declared_validator(tmp_path: Path) -> None:
    p = PhaseDeclaration(
        name="p_typed",
        trigger="before_turn",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="turn",
        order=10,
        version="1",
        failure="continue",
        replay="once",
    )
    received_typed: Any = None

    def validate_input(val: Any) -> dict[str, str]:
        assert isinstance(val, str)
        return {"validated": val.upper()}

    def cb(typed_input: Any, inst: Any, ctx: Any) -> str:
        nonlocal received_typed
        received_typed = typed_input
        return "ok"

    phase_funcs = {
        "test:p_typed": (
            validate_input,
            cb,
            lambda outcome: {"ok": "ok"},
            lambda payload, inst, ctx: payload["ok"],
        ),
    }

    active = _make_active([p], phase_funcs)
    records: dict[str, PhaseRecord] = {}
    active.run_phase(
        "before_turn",
        "lowercase_input",
        scope="turn",
        scope_id="turn-1",
        context=_context(tmp_path),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )
    assert received_typed == {"validated": "LOWERCASE_INPUT"}


def test_tool_scope_components_match_exact_tool(tmp_path: Path) -> None:
    p = PhaseDeclaration(
        name="p_tool",
        trigger="before_tool",
        slot="tool",
        component="phase_tool",
        callback="m:c",
        validate_input="m:v",
        snapshot="m:s",
        restore="m:r",
        scope="tool",
        order=10,
        version="1",
        failure="continue",
        replay="once",
    )
    executed = False

    def cb(inp: Any, inst: Any, ctx: Any) -> str:
        nonlocal executed
        executed = True
        return "tool_ok"

    phase_funcs = {
        "test:p_tool": (
            lambda val: val,
            cb,
            lambda outcome: {"ok": "ok"},
            lambda payload, inst, ctx: payload["ok"],
        ),
    }

    active = _make_active([p], phase_funcs)
    records: dict[str, PhaseRecord] = {}

    # Matching tool executes
    matching_call = ToolCall(tool_name="phase_tool", tool_call_id="call-1", arguments={})
    outcomes = active.run_phase(
        "before_tool",
        matching_call,
        scope="tool",
        scope_id="tool-call-1",
        context=_context(tmp_path, scope="tool", scope_id="tool-call-1"),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )
    assert executed is True
    assert len(outcomes) == 1

    # Non-matching tool does not execute phase
    executed = False
    other_call = ToolCall(tool_name="other_tool", tool_call_id="call-2", arguments={})
    outcomes_other = active.run_phase(
        "before_tool",
        other_call,
        scope="tool",
        scope_id="tool-call-2",
        context=_context(tmp_path, scope="tool", scope_id="tool-call-2"),
        read_record=lambda k: records.get(k),
        write_record=lambda r: records.update({f"{r.phase_key}:{r.scope}:{r.scope_id}": r}),
    )
    assert executed is False
    assert len(outcomes_other) == 0
