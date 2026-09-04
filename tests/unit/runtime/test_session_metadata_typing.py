from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from voidcode.runtime.acp import AcpAdapterState, AcpConfigState
from voidcode.runtime.contracts import (
    PLAN_STATE_METADATA_KEYS,
    RUNTIME_STATE_METADATA_KEYS,
    SKILL_SNAPSHOT_METADATA_KEYS,
)
from voidcode.runtime.execution.provider_execution_metadata import run_id_from_session_metadata
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.session_metadata_helpers import (
    parse_delegation_metadata,
    parse_plan_state_metadata,
    parse_runtime_state_metadata,
    parse_skill_snapshot_metadata,
    persist_tool_execution_intent,
    plan_state_from_metadata,
    runtime_state_context_compacted,
    runtime_state_context_projection,
    runtime_state_context_transform_applied,
    runtime_state_metadata_payload,
    runtime_state_pending_tool_intent,
    runtime_state_run_id,
    runtime_state_todos,
    runtime_state_value,
    session_metadata_with_runtime_state_updates,
    session_with_context_compacted_state,
    session_with_context_transform_applied_state,
    session_with_context_window_payload_metadata,
    session_with_current_acp_metadata,
    session_with_plan_state,
    session_with_run_id,
    session_with_todo_state,
    session_without_tool_intent,
)
from voidcode.runtime.skills import (
    SkillRuntimeContext,
    build_skill_execution_snapshot,
    snapshot_payload,
)
from voidcode.runtime.storage import SessionStore
from voidcode.runtime.todos import todo_state_from_session_metadata


def _runtime_state_payload() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "todos": {"version": 1, "revision": 3, "todos": []},
        "context_compacted": {
            "last_summary_anchor": "anchor-1",
            "last_original_tool_result_count": 2,
            "last_retained_tool_result_count": 1,
            "last_emitted_run_id": "run-1",
        },
        "context_transform_applied": {
            "last_emitted_fingerprints": ["fp-1"],
            "last_emitted_run_id": "run-1",
        },
        "pending_tool_intent": {
            "tool_call_id": "call-1",
            "tool_name": "bash",
            "arguments": {"command": "ls"},
            "replay_policy": "safe",
            "status": "pending",
        },
        "context_projection": {"version": 2, "projection_id": "proj-1", "source_event_sequence": 7},
        "context_projection_summary": {"anchor": "proj-1", "source": "tool_result_window"},
    }


# Current persisted leaf metadata is strict on both reads and writes.
@pytest.mark.parametrize(
    "parse,payload",
    (
        (parse_runtime_state_metadata, {"run_id": "run-1", "typo_field": 1}),
        (parse_plan_state_metadata, {"status": "waiting", "typo_field": 1}),
        (parse_delegation_metadata, {"mode": "sync", "typo_field": 1}),
    ),
)
def test_persisted_parse_rejects_unknown_keys(parse: object, payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="is not supported"):
        cast(object, parse)(payload)  # type: ignore[operator]


@pytest.mark.parametrize(
    "parse,payload",
    (
        (parse_runtime_state_metadata, {"run_id": 42}),
        (parse_runtime_state_metadata, {"todos": []}),
        (parse_plan_state_metadata, {"status": 7}),
        (parse_plan_state_metadata, {"blocked_tool": "bash"}),
        (parse_delegation_metadata, {"mode": "sync", "depth": "3"}),
        (parse_delegation_metadata, {"mode": "invalid"}),
        (parse_delegation_metadata, {"depth": 1}),
    ),
)
def test_persisted_parse_rejects_type_and_missing_fields(parse: object, payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        cast(object, parse)(payload)  # type: ignore[operator]


def test_persisted_parse_rejects_non_dict() -> None:
    with pytest.raises(ValueError, match="persisted runtime_state must be an object"):
        parse_runtime_state_metadata([])
    with pytest.raises(ValueError, match="persisted plan_state must be an object"):
        parse_plan_state_metadata("waiting")
    with pytest.raises(ValueError, match="persisted delegation must be an object"):
        parse_delegation_metadata(None)


def test_persisted_parse_accepts_current_payload_and_copies_input() -> None:
    runtime_state = _runtime_state_payload()
    parsed = parse_runtime_state_metadata(runtime_state)
    assert parsed == runtime_state
    parsed["run_id"] = "mutated"
    assert runtime_state["run_id"] == "run-1"


def test_persisted_parse_accepts_current_delegation_payload() -> None:
    parsed = parse_delegation_metadata(
        {
            "mode": "background",
            "subagent_type": "task",
            "depth": 2,
            "remaining_spawn_budget": 5,
            "selected_preset": "task",
            "selected_execution_engine": "provider",
        }
    )
    assert parsed["depth"] == 2
    assert parsed["mode"] == "background"


@pytest.mark.parametrize(
    "parser,payload",
    (
        (parse_runtime_state_metadata, {"run_id": "run-1", "continuity": {"version": 1}}),
        (parse_plan_state_metadata, {"blocked_tool": "bash"}),
        (parse_delegation_metadata, {"mode": "sync", "depth": "3"}),
    ),
)
def test_legacy_metadata_is_rejected(parser: object, payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        cast(object, parser)(payload)  # type: ignore[operator]


# ---------------------------------------------------------------------------
# 只读 accessor（§5.4）
# ---------------------------------------------------------------------------


def test_runtime_state_run_id_accessor() -> None:
    assert runtime_state_run_id({"runtime_state": {"run_id": "run-1"}}) == "run-1"
    assert runtime_state_run_id({"runtime_state": {}}) is None
    assert runtime_state_run_id({}) is None
    with pytest.raises(ValueError, match="run_id"):
        runtime_state_run_id({"runtime_state": {"run_id": ""}})
    assert run_id_from_session_metadata({"runtime_state": {"run_id": "run-1"}}) == "run-1"
    assert run_id_from_session_metadata({}) is None


def test_runtime_state_accessors() -> None:
    metadata = {"runtime_state": _runtime_state_payload()}
    assert runtime_state_todos(metadata) == {"version": 1, "revision": 3, "todos": []}
    assert runtime_state_pending_tool_intent(metadata) is not None
    assert runtime_state_pending_tool_intent(metadata) is not None and metadata["runtime_state"]["pending_tool_intent"] is not None
    assert runtime_state_context_compacted(metadata) is not None
    assert runtime_state_context_transform_applied(metadata) is not None
    assert runtime_state_context_projection(metadata) is not None
    assert runtime_state_value(metadata, "run_id") == "run-1"
    assert runtime_state_value(metadata, "unknown_field") is None
    assert runtime_state_todos({}) is None
    with pytest.raises(ValueError, match="must be an object"):
        runtime_state_todos({"runtime_state": {"todos": []}})
    assert runtime_state_value({}, "run_id") is None


def test_todo_state_from_session_metadata_via_accessor() -> None:
    metadata = {
        "runtime_state": {
            "todos": {
                "version": 1,
                "revision": 5,
                "todos": [{"content": "task", "status": "pending", "position": 0, "updated_at": 5}],
            }
        }
    }
    todo_state = todo_state_from_session_metadata(metadata)
    assert todo_state is not None
    assert todo_state["revision"] == 5
    with pytest.raises(ValueError, match="must be an object"):
        todo_state_from_session_metadata({"runtime_state": {"todos": "invalid"}})


# ---------------------------------------------------------------------------
# skill_snapshot（恒严格：未知 key 拒绝 + 委托 snapshot_from_payload）
# ---------------------------------------------------------------------------


def _skill_snapshot_payload() -> dict[str, object]:
    from voidcode.runtime.skills import SkillRuntimeContext

    snapshot = build_skill_execution_snapshot(
        [SkillRuntimeContext(name="test", description="d", content="c", prompt_context="p")],
        source="run",
    )
    return snapshot_payload(snapshot)


def test_parse_skill_snapshot_metadata_accepts_valid_payload() -> None:
    payload = _skill_snapshot_payload()
    assert parse_skill_snapshot_metadata(payload) == payload


def test_parse_skill_snapshot_metadata_rejects_unknown_keys() -> None:
    payload = _skill_snapshot_payload()
    payload["sneaky_extra"] = "value"
    with pytest.raises(ValueError, match="is not supported"):
        parse_skill_snapshot_metadata(payload)


def test_parse_skill_snapshot_metadata_rejects_non_dict() -> None:
    with pytest.raises(ValueError, match="persisted skill_snapshot must be an object"):
        parse_skill_snapshot_metadata(None)


def test_parse_skill_snapshot_metadata_delegates_hash_validation() -> None:
    payload = _skill_snapshot_payload()
    payload["snapshot_hash"] = "0" * 64  # 篡改 hash
    with pytest.raises(ValueError, match="hash"):
        parse_skill_snapshot_metadata(payload)


# ---------------------------------------------------------------------------
# key-set 常量与 TypedDict 字段一致（§3）
# ---------------------------------------------------------------------------


def test_key_set_constants() -> None:
    assert RUNTIME_STATE_METADATA_KEYS == frozenset(
        {
            "run_id",
            "acp",
            "context_projection",
            "context_projection_summary",
            "todos",
            "pending_tool_intent",
            "context_compacted",
            "context_transform_applied",
        }
    )
    # legacy 键明确不在写入 key-set
    assert "continuity" not in RUNTIME_STATE_METADATA_KEYS
    assert "continuity_summary" not in RUNTIME_STATE_METADATA_KEYS
    assert PLAN_STATE_METADATA_KEYS == frozenset({"status", "approval_request_id", "blocked_tool", "last_error"})
    assert SKILL_SNAPSHOT_METADATA_KEYS == frozenset(
        {
            "snapshot_version",
            "source",
            "selected_skill_names",
            "applied_skill_payloads",
            "skill_prompt_context",
            "binding_snapshot",
            "snapshot_hash",
        }
    )


# ---------------------------------------------------------------------------
# Phase 2：写路径 strict 拒绝（§6 Phase 2 验收 —— 构造器内置闸）
# ---------------------------------------------------------------------------


def _write_path_session(
    *,
    runtime_state: dict[str, object] | None = None,
    plan_state: dict[str, object] | None = None,
) -> SessionState:
    metadata: dict[str, object] = {}
    if runtime_state is not None:
        metadata["runtime_state"] = runtime_state
    if plan_state is not None:
        metadata["plan_state"] = plan_state
    return SessionState(
        session=SessionRef(id="p2-write-path"),
        status="running",
        turn=1,
        metadata=metadata,
    )


def _acp_state() -> AcpAdapterState:
    return AcpAdapterState(
        mode="managed",
        configuration=AcpConfigState(configured_enabled=True),
        configured=True,
        status="connected",
        available=True,
    )


def test_write_path_constructors_reject_unknown_runtime_state_keys() -> None:
    # 手工往构造器输入未知 key（runtime_state["typo_field"]）→ ValueError
    typo_session = _write_path_session(runtime_state={"run_id": "run-1", "typo_field": 1})
    with pytest.raises(ValueError, match="is not supported"):
        session_with_todo_state(typo_session, raw_todos=[], revision=1)
    with pytest.raises(ValueError, match="is not supported"):
        session_with_run_id(typo_session, run_id="run-2")
    with pytest.raises(ValueError, match="is not supported"):
        session_with_context_compacted_state(
            typo_session,
            summary_anchor="a",
            original_tool_result_count=1,
            retained_tool_result_count=1,
        )
    with pytest.raises(ValueError, match="is not supported"):
        session_with_context_transform_applied_state(typo_session, fingerprints=("fp",))
    with pytest.raises(ValueError, match="is not supported"):
        session_with_current_acp_metadata(typo_session, _acp_state())
    with pytest.raises(ValueError, match="is not supported"):
        session_with_context_window_payload_metadata(
            typo_session,
            {"projection": None, "summary_anchor": None},
        )
    with pytest.raises(ValueError, match="is not supported"):
        session_metadata_with_runtime_state_updates(
            typo_session.metadata,
            updates={"todos": {"version": 1, "revision": 1, "todos": []}},
        )
    with pytest.raises(ValueError, match="is not supported"):
        persist_tool_execution_intent(
            cast(SessionStore, None),
            Path("."),
            typo_session,
            intent={"tool_call_id": "call-1"},
        )


def test_session_without_tool_intent_rejects_unknown_runtime_state_keys() -> None:
    # 有 pending_tool_intent 需要清理时，strict 闸对整个合并 payload 生效
    typo_session = _write_path_session(
        runtime_state={
            "typo_field": 1,
            "pending_tool_intent": {"tool_call_id": "call-1"},
        }
    )
    with pytest.raises(ValueError, match="is not supported"):
        session_without_tool_intent(typo_session)


def test_plan_state_write_gate_rejects_unknown_keys_and_statuses() -> None:
    with pytest.raises(ValueError, match="is not supported"):
        plan_state_from_metadata({"plan_state": {"status": "waiting", "typo_field": 1}})
    with pytest.raises(ValueError, match="is not supported"):
        session_with_plan_state(
            _write_path_session(plan_state={"status": "waiting", "typo_field": 1}),
            status="waiting_approval",
        )
    with pytest.raises(ValueError, match="status"):
        plan_state_from_metadata({"plan_state": {"status": "bogus"}})
    # 合法 status 全部通过
    for status in (
        "waiting",
        "waiting_approval",
        "waiting_question",
        "in_progress",
        "completed",
        "interrupted",
        "failed",
    ):
        assert plan_state_from_metadata({"plan_state": {"status": status}})["status"] == status


def test_delegation_strict_write_gate_rejects_invalid_depth() -> None:
    valid = parse_delegation_metadata({"mode": "sync", "subagent_type": "task", "depth": 2, "remaining_spawn_budget": 3})
    assert valid["depth"] == 2
    assert valid["remaining_spawn_budget"] == 3
    for bad_depth in (-1, "3", 2.5):
        with pytest.raises(ValueError, match="non-negative integer"):
            parse_delegation_metadata({"mode": "sync", "depth": bad_depth})
    with pytest.raises(ValueError, match="non-negative integer"):
        parse_delegation_metadata({"mode": "sync", "remaining_spawn_budget": -1})


# ---------------------------------------------------------------------------
# Phase 2：字节等价（构造器输出与迁移前手工构造完全一致）
# ---------------------------------------------------------------------------


def test_runtime_state_metadata_payload_matches_manual_construction() -> None:
    acp_state = _acp_state()
    expected_acp = {
        "mode": "managed",
        "configured_enabled": True,
        "status": "connected",
        "available": True,
        "last_error": None,
        "last_request_type": None,
        "last_request_id": None,
        "last_event_type": None,
        "last_delegation": None,
    }
    assert runtime_state_metadata_payload(run_id="run-1", acp_state=acp_state) == {
        "run_id": "run-1",
        "acp": expected_acp,
    }
    # run_id=None 时不写 run_id key（service.py:5921-5942 原语义）
    assert runtime_state_metadata_payload(run_id=None, acp_state=acp_state) == {
        "acp": expected_acp,
    }


def test_session_with_run_id_matches_manual_merge() -> None:
    session = _write_path_session(runtime_state={"run_id": "run-1"})
    updated = session_with_run_id(session, run_id="run-2")
    assert updated.metadata == {"runtime_state": {"run_id": "run-2"}}
    assert updated.session is session.session
    assert updated.turn == session.turn
    # run_id=None：仅净化层生效，不写 run_id
    assert session_with_run_id(session, run_id=None).metadata == {"runtime_state": {"run_id": "run-1"}}


def test_session_with_context_compacted_state_matches_manual_construction() -> None:
    session = _write_path_session(runtime_state={"run_id": "run-1"})
    updated = session_with_context_compacted_state(
        session,
        summary_anchor="anchor-1",
        original_tool_result_count=2,
        retained_tool_result_count=1,
    )
    assert updated.metadata["runtime_state"] == {
        "run_id": "run-1",
        "context_compacted": {
            "last_summary_anchor": "anchor-1",
            "last_original_tool_result_count": 2,
            "last_retained_tool_result_count": 1,
            "last_emitted_run_id": "run-1",
        },
    }


def test_session_with_context_transform_applied_state_matches_manual_construction() -> None:
    session = _write_path_session(runtime_state={"run_id": "run-1"})
    updated = session_with_context_transform_applied_state(session, fingerprints=("fp-2",))
    assert updated.metadata["runtime_state"] == {
        "run_id": "run-1",
        "context_transform_applied": {
            "last_emitted_fingerprints": ["fp-2"],
            "last_emitted_run_id": "run-1",
        },
    }
    # 同 run：合并既有 fingerprints 并排序
    merged = _write_path_session(
        runtime_state={
            "run_id": "run-1",
            "context_transform_applied": {
                "last_emitted_fingerprints": ["fp-1"],
                "last_emitted_run_id": "run-1",
            },
        }
    )
    merged_updated = session_with_context_transform_applied_state(merged, fingerprints=("fp-3", "fp-0"))
    assert merged_updated.metadata["runtime_state"]["context_transform_applied"] == {
        "last_emitted_fingerprints": ["fp-0", "fp-1", "fp-3"],
        "last_emitted_run_id": "run-1",
    }
    # 跨 run：不继承旧 run 的 fingerprints
    cross_run = _write_path_session(
        runtime_state={
            "run_id": "run-2",
            "context_transform_applied": {
                "last_emitted_fingerprints": ["fp-1"],
                "last_emitted_run_id": "run-1",
            },
        }
    )
    cross_updated = session_with_context_transform_applied_state(cross_run, fingerprints=("fp-9",))
    assert cross_updated.metadata["runtime_state"]["context_transform_applied"] == {
        "last_emitted_fingerprints": ["fp-9"],
        "last_emitted_run_id": "run-2",
    }


def test_session_without_tool_intent_matches_manual_pop() -> None:
    session = _write_path_session(runtime_state={"pending_tool_intent": {"tool_call_id": "call-1"}})
    cleaned = session_without_tool_intent(session)
    assert cleaned is not session
    assert cleaned.metadata["runtime_state"] == {}
    assert cleaned.session is session.session
    # 无 pending_tool_intent 时原样返回同一对象（调用方身份判断）
    plain = _write_path_session(runtime_state={"run_id": "run-1"})
    assert session_without_tool_intent(plain) is plain


def test_snapshot_to_session_metadata_gates_and_matches_snapshot_payload() -> None:
    from voidcode.runtime.skill_metadata import snapshot_to_session_metadata

    snapshot = build_skill_execution_snapshot(
        [SkillRuntimeContext(name="test", description="d", content="c", prompt_context="p")],
        source="run",
    )
    metadata = snapshot_to_session_metadata(snapshot)
    # 输出经 parse_skill_snapshot_metadata 校验（顶层未知 key 拒绝）
    parsed = parse_skill_snapshot_metadata(cast(dict[str, object], metadata["skill_snapshot"]))
    assert parsed["snapshot_version"] == 1
    assert metadata["selected_skill_names"] == ["test"]
    assert metadata["applied_skills"] == ["test"]
    # 与迁移前 snapshot_payload 输出字节等价
    assert metadata["skill_snapshot"] == snapshot_payload(snapshot)
