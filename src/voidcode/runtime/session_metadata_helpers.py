from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .context_window import (
    ContextProjection,
    RuntimeContextWindow,
    continuity_state_from_metadata_payload,
)
from .contracts import (
    DELEGATION_METADATA_KEYS,
    PLAN_STATE_METADATA_KEYS,
    RUNTIME_STATE_METADATA_KEYS,
    SKILL_SNAPSHOT_METADATA_KEYS,
    AcpStateMetadata,
    ContextCompactedStateMetadata,
    ContextProjectionMetadata,
    ContextTransformAppliedStateMetadata,
    PendingToolIntentMetadata,
    PersistedDelegationMetadata,
    PlanStateMetadata,
    RuntimeResponse,
    RuntimeStateMetadata,
    SkillSnapshotMetadata,
    TodosStateMetadata,
    UnknownSessionError,
)
from .permission import DelegationGovernance
from .permission_policy import (
    pending_approval_from_response,
    pending_question_from_response,
)
from .session import (
    SessionState,
    session_metadata_for_persistence,
    validate_session_workspace,
)
from .skills import snapshot_from_payload
from .todos import (
    runtime_todos_equal,
    runtime_todos_from_state_payload,
    runtime_todos_from_tool_payload,
    todo_event_payload,
    todo_state_payload,
)

if TYPE_CHECKING:
    from .acp import AcpAdapterState
    from .storage import SessionStore

logger = logging.getLogger(__name__)

_DELEGATION_GOVERNANCE = DelegationGovernance()

# 写路径允许的 plan_state.status 取值（§3.2 枚举；读路径 lenient 不限制，
# 旧数据状态由消费点现有 guard 容忍）。
_PLAN_STATE_STATUSES = frozenset(
    {
        "waiting",
        "waiting_approval",
        "waiting_question",
        "in_progress",
        "completed",
        "interrupted",
        "failed",
    }
)


def _coerce_int_like(value: object | None, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _reject_unknown_metadata_keys(
    payload: dict[str, object],
    *,
    allowed_keys: frozenset[str],
    structure_name: str,
) -> None:
    unknown_keys = sorted(key for key in payload if key not in allowed_keys)
    if unknown_keys:
        raise ValueError(f"persisted {structure_name} field '{unknown_keys[0]}' is not supported")


def _validate_runtime_state_metadata_types(payload: dict[str, object]) -> None:
    if "run_id" in payload and not isinstance(payload["run_id"], str):
        raise ValueError("persisted runtime_state field 'run_id' must be a string")
    for field in (
        "acp",
        "context_projection",
        "context_projection_summary",
        "todos",
        "pending_tool_intent",
        "context_compacted",
        "context_transform_applied",
    ):
        if field in payload and not isinstance(payload[field], dict):
            raise ValueError(f"persisted runtime_state field '{field}' must be an object")


def _validate_plan_state_metadata_types(payload: dict[str, object]) -> None:
    for field in ("status", "approval_request_id", "blocked_tool", "last_error"):
        if field in payload and not isinstance(payload[field], str):
            raise ValueError(f"persisted plan_state field '{field}' must be a string")
    if "status" in payload and payload["status"] not in _PLAN_STATE_STATUSES:
        joined = ", ".join(sorted(_PLAN_STATE_STATUSES))
        raise ValueError(f"persisted plan_state field 'status' must be one of: {joined}")


def _validate_delegation_metadata_types(payload: dict[str, object]) -> None:
    for field in ("subagent_type", "description", "command", "selected_preset", "selected_execution_engine", "parallel_group_id"):
        if field in payload and not isinstance(payload[field], str):
            raise ValueError(f"persisted delegation field '{field}' must be a string")
    if "mode" in payload and payload["mode"] not in {"sync", "background"}:
        raise ValueError("persisted delegation field 'mode' must be one of: sync, background")
    for field in ("depth", "remaining_spawn_budget", "parallel_group_size"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"persisted delegation field '{field}' must be a non-negative integer")
    if "output_schema" in payload and not isinstance(payload["output_schema"], dict):
        raise ValueError("persisted delegation field 'output_schema' must be an object")
    if "schema_mode" in payload and payload["schema_mode"] not in {"permissive", "strict"}:
        raise ValueError("persisted delegation field 'schema_mode' must be one of: permissive, strict")


def parse_runtime_state_metadata(raw: object, *, strict: bool = False) -> RuntimeStateMetadata:
    """Parse persisted ``session.metadata["runtime_state"]``.

    ``strict=True``（写路径 / 新构造）：``raw`` 必须是 dict；未知 key 与
    已知字段类型不符抛 ``ValueError``（仿 config_materializer 的
    ``_reject_unknown_keys`` 文案）。

    ``strict=False``（读路径，默认）：非 dict 返回 ``{}``（等价现有
    ``cast(...) if isinstance(...) else {}`` 容错）；未知 key 原样保留
    （round-trip 安全，绝不 drop）；已知字段类型不符不抛——容忍语义由
    各消费点既有 guard 承担，与本函数迁移前逐点等价。返回的 dict 是输入
    的浅拷贝，不突变输入。
    """
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("persisted runtime_state must be an object")
        return {}
    payload = dict(raw)
    if strict:
        _reject_unknown_metadata_keys(
            payload,
            allowed_keys=RUNTIME_STATE_METADATA_KEYS,
            structure_name="runtime_state",
        )
        _validate_runtime_state_metadata_types(payload)
    return cast(RuntimeStateMetadata, payload)


def parse_plan_state_metadata(raw: object, *, strict: bool = False) -> PlanStateMetadata:
    """Parse persisted ``session.metadata["plan_state"]``（语义同
    ``parse_runtime_state_metadata`` 的双模约定）。"""
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("persisted plan_state must be an object")
        return {}
    payload = dict(raw)
    if strict:
        _reject_unknown_metadata_keys(
            payload,
            allowed_keys=PLAN_STATE_METADATA_KEYS,
            structure_name="plan_state",
        )
        _validate_plan_state_metadata_types(payload)
    return cast(PlanStateMetadata, payload)


def parse_delegation_metadata(raw: object, *, strict: bool = False) -> PersistedDelegationMetadata:
    """Parse persisted ``session.metadata["delegation"]``（语义同
    ``parse_runtime_state_metadata`` 的双模约定）。

    与请求侧 ``validate_runtime_subagent_routing_metadata``（抛
    ``RuntimeRequestError``）分离：persisted 读侧独立、宽容、不抛
    ``RuntimeRequestError``（resume 旧 session 缺 ``mode`` 不应整体拒绝）。
    ``depth``/``remaining_spawn_budget`` 读侧沿用 ``_coerce_int_like``
    宽容语义；strict 写路径要求真 int 且 >= 0。
    """
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("persisted delegation must be an object")
        return {}
    payload = dict(raw)
    if strict:
        _reject_unknown_metadata_keys(
            payload,
            allowed_keys=DELEGATION_METADATA_KEYS,
            structure_name="delegation",
        )
        _validate_delegation_metadata_types(payload)
    return cast(PersistedDelegationMetadata, payload)


def parse_skill_snapshot_metadata(raw: object) -> SkillSnapshotMetadata:
    """Parse persisted ``session.metadata["skill_snapshot"]``（恒严格）。

    snapshot 已显式版本化（``snapshot_version: 1``）+ hash 校验（skills.py
    ``snapshot_from_payload``）；本函数在其之前补顶层未知 key 拒绝——现状
    缺口是 hash 只覆盖 6 个已知字段，payload 多塞未知 key 不会破坏 hash、
    会静默通过。skills.py 的 parser 本身不改（hash 语义与字节格式是另一
    契约），只在 helpers 包装层加拒绝。
    """
    if not isinstance(raw, dict):
        raise ValueError("persisted skill_snapshot must be an object")
    payload = dict(raw)
    _reject_unknown_metadata_keys(
        payload,
        allowed_keys=SKILL_SNAPSHOT_METADATA_KEYS,
        structure_name="skill_snapshot",
    )
    _ = snapshot_from_payload(payload)  # version / hash / 类型全量校验（现状语义）
    return cast(SkillSnapshotMetadata, payload)


def _runtime_state_payload(metadata: Mapping[str, object]) -> RuntimeStateMetadata:
    return parse_runtime_state_metadata(metadata.get("runtime_state"))


def runtime_state_run_id(metadata: Mapping[str, object]) -> str | None:
    """Persisted ``runtime_state.run_id``（str 原样返回，含空串；非 str 为 None）。

    需要非空语义的调用点（如 ``_current_run_id``）自行过滤空串，与迁移前
    逐点等价。
    """
    run_id = _runtime_state_payload(metadata).get("run_id")
    return run_id if isinstance(run_id, str) else None


def runtime_state_acp(metadata: Mapping[str, object]) -> AcpStateMetadata | None:
    value = _runtime_state_payload(metadata).get("acp")
    return value if isinstance(value, dict) else None


def runtime_state_todos(metadata: Mapping[str, object]) -> TodosStateMetadata | None:
    value = _runtime_state_payload(metadata).get("todos")
    return value if isinstance(value, dict) else None


def runtime_state_pending_tool_intent(metadata: Mapping[str, object]) -> PendingToolIntentMetadata | None:
    value = _runtime_state_payload(metadata).get("pending_tool_intent")
    return value if isinstance(value, dict) else None


def runtime_state_context_compacted(metadata: Mapping[str, object]) -> ContextCompactedStateMetadata | None:
    value = _runtime_state_payload(metadata).get("context_compacted")
    return value if isinstance(value, dict) else None


def runtime_state_context_transform_applied(metadata: Mapping[str, object]) -> ContextTransformAppliedStateMetadata | None:
    value = _runtime_state_payload(metadata).get("context_transform_applied")
    return value if isinstance(value, dict) else None


def runtime_state_context_projection(metadata: Mapping[str, object]) -> ContextProjectionMetadata | None:
    value = _runtime_state_payload(metadata).get("context_projection")
    return value if isinstance(value, dict) else None


def runtime_state_context_projection_summary(metadata: Mapping[str, object]) -> dict[str, str] | None:
    value = _runtime_state_payload(metadata).get("context_projection_summary")
    return value if isinstance(value, dict) else None


def runtime_state_value(metadata: Mapping[str, object], key: str) -> object | None:
    """Generic ``runtime_state`` field read（``_prompt_activation_this_run``
    等同层读取，不进 key-set）。"""
    return _runtime_state_payload(metadata).get(key)


def _acp_state_payload(acp_state: AcpAdapterState) -> dict[str, object]:
    """Serialize ``AcpAdapterState`` to the persisted ``runtime_state.acp``
    payload（service ``_runtime_state_metadata`` / ``_runtime_state_metadata_with_acp_state``
    共用，两处构造一致）。"""
    return {
        "mode": acp_state.mode,
        "configured_enabled": acp_state.configuration.configured_enabled,
        "status": acp_state.status,
        "available": acp_state.available,
        "last_error": acp_state.last_error,
        "last_request_type": acp_state.last_request_type,
        "last_request_id": acp_state.last_request_id,
        "last_event_type": acp_state.last_event_type,
        "last_delegation": (acp_state.last_delegation.as_payload() if acp_state.last_delegation is not None else None),
    }


def _runtime_state_payload_with_updates(
    metadata: Mapping[str, object],
    *,
    updates: Mapping[str, object] | None = None,
    removed: frozenset[str] = frozenset(),
) -> RuntimeStateMetadata:
    """Merge ``updates`` / ``removed`` into the persisted ``runtime_state`` and
    gate the result through ``parse_runtime_state_metadata(..., strict=True)``.

    写路径唯一合并入口：所有 session 级写构造器与 storage 的 metadata 级写
    都经此合并 + strict 闸（未知 key / 类型不符拒绝）。非 dict 的存量
    ``runtime_state`` 按读路径容错语义视为 ``{}``（与迁移前各构造点一致）。
    """
    raw_runtime_state = metadata.get("runtime_state")
    runtime_state = dict(cast(dict[str, object], raw_runtime_state)) if isinstance(raw_runtime_state, dict) else {}
    if updates:
        runtime_state.update(updates)
    for key in removed:
        runtime_state.pop(key, None)
    return parse_runtime_state_metadata(runtime_state, strict=True)


def runtime_state_metadata_payload(
    *,
    run_id: str | None = None,
    acp_state: AcpAdapterState,
) -> RuntimeStateMetadata:
    """Fresh ``runtime_state`` payload for a new run（service
    ``_runtime_state_metadata`` 迁入；strict 写闸内置）。"""
    payload = {
        **({"run_id": run_id} if run_id is not None else {}),
        "acp": _acp_state_payload(acp_state),
    }
    return parse_runtime_state_metadata(payload, strict=True)


def session_with_run_id(
    session: SessionState,
    *,
    run_id: str | None,
) -> SessionState:
    """Return ``session`` with persisted ``runtime_state.run_id`` set（resume
    ``_metadata_with_resume_run_id`` 迁入；先经持久化净化层再 strict 写闸）。
    ``run_id`` 为 ``None`` 时仅应用净化层、不写 run_id，与迁移前一致。"""
    persisted = session_metadata_for_persistence(session.metadata)
    if run_id is None:
        return _session_with_metadata(session, persisted)
    runtime_state = _runtime_state_payload_with_updates(persisted, updates={"run_id": run_id})
    return _session_with_metadata(session, {**persisted, "runtime_state": runtime_state})


def session_with_context_compacted_state(
    session: SessionState,
    *,
    summary_anchor: str | None,
    original_tool_result_count: int,
    retained_tool_result_count: int,
) -> SessionState:
    """Persist ``runtime_state.context_compacted``（run_loop
    ``_session_with_context_compacted_state`` 迁入；strict 写闸内置）。"""
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={
            "context_compacted": {
                "last_summary_anchor": summary_anchor,
                "last_original_tool_result_count": original_tool_result_count,
                "last_retained_tool_result_count": retained_tool_result_count,
                "last_emitted_run_id": runtime_state_run_id(session.metadata),
            },
        },
    )
    return _session_with_metadata(session, {**session.metadata, "runtime_state": runtime_state})


def session_with_context_transform_applied_state(
    session: SessionState,
    *,
    fingerprints: tuple[str, ...],
) -> SessionState:
    """Persist ``runtime_state.context_transform_applied``（run_loop
    ``_session_with_context_transform_applied_state`` 迁入；strict 写闸内置）。"""
    current_run_id = runtime_state_run_id(session.metadata)
    transform_state = runtime_state_context_transform_applied(session.metadata) or {}
    last_run_id = transform_state.get("last_emitted_run_id")
    last_run_id = last_run_id if isinstance(last_run_id, str) else None
    existing_fingerprints: set[str] = set()
    if current_run_id is None or last_run_id == current_run_id:
        raw_existing = transform_state.get("last_emitted_fingerprints")
        if isinstance(raw_existing, list):
            existing_fingerprints = {item for item in raw_existing if isinstance(item, str) and item.strip()}
    existing_fingerprints.update(fingerprints)
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={
            "context_transform_applied": {
                "last_emitted_fingerprints": sorted(existing_fingerprints),
                "last_emitted_run_id": current_run_id,
            },
        },
    )
    return _session_with_metadata(session, {**session.metadata, "runtime_state": runtime_state})


def session_without_tool_intent(session: SessionState) -> SessionState:
    """Return ``session`` with ``runtime_state.pending_tool_intent`` removed
    （service ``persist_response`` 清理迁入；无该 key 时原样返回同一对象，
    调用方可按身份判断是否有写）。"""
    if "pending_tool_intent" not in _runtime_state_payload(session.metadata):
        return session
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        removed=frozenset({"pending_tool_intent"}),
    )
    return _session_with_metadata(session, {**session.metadata, "runtime_state": runtime_state})


def session_metadata_with_runtime_state_updates(
    metadata: dict[str, object],
    *,
    updates: Mapping[str, object] | None = None,
    removed: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Return ``metadata`` with ``runtime_state`` mutated through the typed
    write path（strict 闸内置）。storage 的 todo 写 / active-revert 写共用，
    消除 storage 侧第二份 ``runtime_state`` 构造。"""
    runtime_state = _runtime_state_payload_with_updates(metadata, updates=updates, removed=removed)
    return {**metadata, "runtime_state": runtime_state}


def session_model_identity(
    metadata: Mapping[str, object],
) -> tuple[str | None, str | None]:
    """Return ``(model, provider)`` resolved from session metadata, if known.

    ``model`` is the configured model reference (``provider/model`` or a bare
    model name) and ``provider`` the resolved provider id from the active
    provider target. Both are ``None`` when the metadata does not carry them.
    """
    runtime_config = metadata.get("runtime_config")
    if not isinstance(runtime_config, Mapping):
        return None, None
    model = runtime_config.get("model")
    if not isinstance(model, str) or not model:
        model = None
    provider: str | None = None
    resolved_provider = runtime_config.get("resolved_provider")
    if isinstance(resolved_provider, Mapping):
        active_target = resolved_provider.get("active_target")
        if isinstance(active_target, Mapping):
            raw_provider = active_target.get("provider")
            if isinstance(raw_provider, str) and raw_provider:
                provider = raw_provider
            if model is None:
                raw_model = active_target.get("raw_model")
                if isinstance(raw_model, str) and raw_model:
                    model = raw_model
    return model, provider


def plan_state_from_metadata(
    metadata: dict[str, object],
    *,
    status: str | None = None,
    approval_request_id: str | None = None,
    blocked_tool: str | None = None,
    error: str | None = None,
) -> dict[str, object] | None:
    existing_plan_state = metadata.get("plan_state")
    if existing_plan_state is None:
        return None
    if not isinstance(existing_plan_state, dict):
        raise ValueError("persisted plan_state must be an object")
    plan_state: dict[str, object] = dict(cast(dict[str, object], existing_plan_state))

    if status is not None:
        plan_state["status"] = status

    if approval_request_id is not None:
        plan_state["approval_request_id"] = approval_request_id
    else:
        plan_state.pop("approval_request_id", None)

    if blocked_tool is not None:
        plan_state["blocked_tool"] = blocked_tool
    else:
        plan_state.pop("blocked_tool", None)

    if error is not None:
        plan_state["last_error"] = error
    else:
        plan_state.pop("last_error", None)

    return cast(
        dict[str, object],
        parse_plan_state_metadata(plan_state, strict=True),
    )


def session_with_context_window_payload_metadata(
    session: SessionState,
    context_window_payload: dict[str, object],
) -> SessionState:
    if "continuity_state" in context_window_payload:
        raise ValueError("legacy continuity_state context metadata is no longer supported")
    raw_runtime_state = session.metadata.get("runtime_state")
    if raw_runtime_state is not None and not isinstance(raw_runtime_state, dict):
        raise ValueError("persisted runtime_state must be an object")
    continuity_payload_raw = context_window_payload.get("projection")
    continuity_payload = cast(dict[str, object], continuity_payload_raw) if isinstance(continuity_payload_raw, dict) else None
    summary_anchor = context_window_payload.get("summary_anchor")
    summary_source = context_window_payload.get("summary_source")
    continuity_summary_payload = (
        {
            "anchor": summary_anchor,
            "source": summary_source,
        }
        if isinstance(summary_anchor, str)
        else None
    )
    metadata = dict(session.metadata)
    raw_prompt_activation = context_window_payload.get("prompt_activation")
    if isinstance(raw_prompt_activation, dict):
        prompt_activation = dict(cast(dict[str, object], raw_prompt_activation))
        raw_runtime_policy = metadata.get("runtime_policy")
        runtime_policy = dict(cast(dict[str, object], raw_runtime_policy)) if isinstance(raw_runtime_policy, dict) else {}
        runtime_policy["prompt_activation"] = prompt_activation
        metadata["runtime_policy"] = runtime_policy
    runtime_state = _runtime_state_payload_with_updates(
        metadata,
        updates={
            **({"context_projection": continuity_payload} if continuity_payload is not None else {}),
            **({"context_projection_summary": continuity_summary_payload} if continuity_summary_payload is not None else {}),
        },
    )
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **metadata,
            "context_window": context_window_payload,
            "runtime_state": runtime_state,
        },
    )


def session_with_todo_state(
    session: SessionState,
    *,
    raw_todos: object,
    revision: int,
) -> tuple[SessionState, dict[str, object]]:
    todos = runtime_todos_from_tool_payload(raw_todos, updated_at=revision)
    state_payload = todo_state_payload(todos, revision=revision)
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={"todos": state_payload},
    )
    next_session = SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={
            **session.metadata,
            "runtime_state": runtime_state,
        },
    )
    event_payload = todo_event_payload(
        session_id=session.session.id,
        todos=todos,
        revision=revision,
    )
    return next_session, event_payload


def todo_state_matches_payload(
    session: SessionState,
    *,
    raw_todos: object,
    revision: int,
) -> bool:
    todo_state = runtime_state_todos(session.metadata)
    if todo_state is None:
        return False
    current = runtime_todos_from_state_payload(todo_state.get("todos"))
    return runtime_todos_equal(current, raw_todos=raw_todos, updated_at=revision)


def _session_with_metadata(session: SessionState, metadata: dict[str, object]) -> SessionState:
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata=metadata,
    )


def session_with_plan_state(
    session: SessionState,
    *,
    status: str | None = None,
    approval_request_id: str | None = None,
    blocked_tool: str | None = None,
    error: str | None = None,
) -> SessionState:
    plan_state = plan_state_from_metadata(
        session.metadata,
        status=status,
        approval_request_id=approval_request_id,
        blocked_tool=blocked_tool,
        error=error,
    )
    if plan_state is None:
        if status is not None and status.startswith("waiting_"):
            plan_state: dict[str, object] = {"status": status}
            if approval_request_id is not None:
                plan_state["approval_request_id"] = approval_request_id
            if blocked_tool is not None:
                plan_state["blocked_tool"] = blocked_tool
            if error is not None:
                plan_state["last_error"] = error
            plan_state = cast(
                dict[str, object],
                parse_plan_state_metadata(plan_state, strict=True),
            )
        else:
            return session
    return _session_with_metadata(
        session,
        {
            **session.metadata,
            "plan_state": plan_state,
        },
    )


def session_with_context_window_metadata(
    session: SessionState,
    context_window: RuntimeContextWindow,
) -> SessionState:
    return session_with_context_window_payload_metadata(session, context_window.metadata_payload())


def delegation_depth_from_metadata(metadata: dict[str, object] | None) -> int:
    if metadata is None:
        return 0
    delegation = parse_delegation_metadata(metadata.get("delegation"))
    return max(0, _coerce_int_like(delegation.get("depth"), 0))


def remaining_spawn_budget_from_metadata(metadata: dict[str, object] | None) -> int:
    if metadata is None:
        return _DELEGATION_GOVERNANCE.spawn_budget
    delegation = parse_delegation_metadata(metadata.get("delegation"))
    remaining = _coerce_int_like(
        delegation.get("remaining_spawn_budget"),
        _DELEGATION_GOVERNANCE.spawn_budget,
    )
    return max(0, remaining)


def continuity_state_from_session_metadata(
    session_metadata: dict[str, object],
) -> ContextProjection | None:
    continuity = runtime_state_context_projection(session_metadata)
    if continuity is None:
        return None
    return continuity_state_from_metadata_payload(continuity)


def _runtime_state_metadata_with_acp_state(
    metadata: dict[str, object],
    acp_state: AcpAdapterState,
) -> dict[str, object]:
    runtime_state_metadata = _runtime_state_payload_with_updates(
        metadata,
        updates={"acp": _acp_state_payload(acp_state)},
    )
    return {**metadata, "runtime_state": runtime_state_metadata}


def session_with_current_acp_metadata(
    session: SessionState,
    acp_state: AcpAdapterState,
) -> SessionState:
    return _session_with_metadata(
        session,
        _runtime_state_metadata_with_acp_state(
            session.metadata,
            acp_state,
        ),
    )


def persist_tool_execution_intent(
    store: SessionStore,
    workspace: Path,
    session: SessionState,
    intent: dict[str, object],
) -> None:
    pending = dict(intent)
    runtime_state = _runtime_state_payload_with_updates(
        session.metadata,
        updates={"pending_tool_intent": pending},
    )
    metadata = {**session.metadata, "runtime_state": runtime_state}
    try:
        store.update_session_metadata(
            workspace=workspace,
            session_id=session.session.id,
            metadata=metadata,
        )
    except UnknownSessionError:
        # The initial run snapshot may not have committed yet.
        logger.debug("tool intent persistence deferred for new session %s", session.session.id)


def clear_tool_execution_intent(
    store: SessionStore,
    workspace: Path,
    session: SessionState,
) -> None:
    try:
        persisted_session = store.load_session(
            workspace=workspace,
            session_id=session.session.id,
        ).session
    except UnknownSessionError:
        logger.debug("tool intent cleanup deferred for new session %s", session.session.id)
        return
    validate_session_workspace(persisted_session, session_id=session.session.id, workspace=workspace)
    runtime_state = persisted_session.metadata.get("runtime_state")
    if not isinstance(runtime_state, dict) or "pending_tool_intent" not in runtime_state:
        return
    state = _runtime_state_payload_with_updates(
        persisted_session.metadata,
        removed=frozenset({"pending_tool_intent"}),
    )
    try:
        store.update_session_metadata(
            workspace=workspace,
            session_id=session.session.id,
            metadata={**persisted_session.metadata, "runtime_state": state},
        )
    except UnknownSessionError:
        logger.debug("tool intent cleanup deferred for new session %s", session.session.id)


def waiting_reason_from_session(session: SessionState) -> str:
    plan_state = parse_plan_state_metadata(session.metadata.get("plan_state"))
    status = plan_state.get("status")
    if status == "waiting_approval":
        return "waiting_for_approval"
    if status == "waiting_question":
        return "waiting_for_question"
    return "waiting"


def resume_waiting_reason(response: RuntimeResponse) -> str:
    try:
        pending_approval_from_response(response)
    except ValueError:
        pass
    else:
        return "waiting_for_approval"
    if pending_question_from_response(response) is not None:
        return "waiting_for_question"
    return "waiting"


__all__ = [
    "DELEGATION_METADATA_KEYS",
    "PLAN_STATE_METADATA_KEYS",
    "PersistedDelegationMetadata",
    "PlanStateMetadata",
    "RUNTIME_STATE_METADATA_KEYS",
    "RuntimeStateMetadata",
    "SKILL_SNAPSHOT_METADATA_KEYS",
    "SkillSnapshotMetadata",
    "clear_tool_execution_intent",
    "continuity_state_from_session_metadata",
    "delegation_depth_from_metadata",
    "parse_delegation_metadata",
    "parse_plan_state_metadata",
    "parse_runtime_state_metadata",
    "parse_skill_snapshot_metadata",
    "persist_tool_execution_intent",
    "plan_state_from_metadata",
    "remaining_spawn_budget_from_metadata",
    "resume_waiting_reason",
    "runtime_state_acp",
    "runtime_state_context_compacted",
    "runtime_state_context_projection",
    "runtime_state_context_projection_summary",
    "runtime_state_context_transform_applied",
    "runtime_state_metadata_payload",
    "runtime_state_pending_tool_intent",
    "runtime_state_run_id",
    "runtime_state_todos",
    "runtime_state_value",
    "session_metadata_with_runtime_state_updates",
    "session_model_identity",
    "session_with_context_compacted_state",
    "session_with_context_transform_applied_state",
    "session_with_context_window_metadata",
    "session_with_context_window_payload_metadata",
    "session_with_current_acp_metadata",
    "session_with_plan_state",
    "session_with_run_id",
    "session_with_todo_state",
    "session_without_tool_intent",
    "todo_state_matches_payload",
    "waiting_reason_from_session",
]
