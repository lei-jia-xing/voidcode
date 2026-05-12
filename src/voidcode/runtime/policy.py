from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

POLICY_SCHEMA_VERSION = 1
POLICY_VERSION = "v1"
PRODUCT_DELEGATION_DENIAL_REASON = "delegation_denied_product_top_level_only"
_ALLOWED_CHILD_PRESETS = ("advisor", "explore", "researcher", "worker")
_ALLOWED_HOOK_ACTIONS = frozenset({"observe", "report", "cancel", "guidance"})
_ALLOWED_HOOK_SCOPES = (
    "session_start",
    "session_end",
    "pre_tool",
    "post_tool",
    "background_task_registered",
    "background_task_started",
    "background_task_progress",
    "background_task_completed",
    "background_task_failed",
    "background_task_cancelled",
    "background_task_notification_enqueued",
    "background_task_result_read",
    "delegated_result_available",
    "context_pressure",
    "turn_progress",
    "stuck_detected",
)
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "env",
    "password",
    "prompt",
    "secret",
    "skill_body",
    "token",
)
_DIAGNOSTIC_LIMIT = 32
_INTENT_LABEL_UNSPECIFIED = "unspecified"


class RuntimePolicySnapshotVersionError(ValueError):
    """Raised when an explicit runtime policy snapshot uses an unsupported version."""


@dataclass(frozen=True, slots=True)
class RuntimePolicyToolPolicyConfig:
    default: str | None = None
    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimePolicyDelegationPolicyConfig:
    default: str | None = None
    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimePolicyHookPolicyConfig:
    allowed_event_scopes: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimePolicyPromptActivationConfig:
    enabled: bool = True
    profile_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimePolicyConfig:
    schema_version: int = POLICY_SCHEMA_VERSION
    version: str = POLICY_VERSION
    enabled: bool = True
    tool_policy: RuntimePolicyToolPolicyConfig = field(
        default_factory=RuntimePolicyToolPolicyConfig
    )
    delegation_policy: RuntimePolicyDelegationPolicyConfig = field(
        default_factory=RuntimePolicyDelegationPolicyConfig
    )
    hook_policy: RuntimePolicyHookPolicyConfig = field(
        default_factory=RuntimePolicyHookPolicyConfig
    )
    prompt_activation: RuntimePolicyPromptActivationConfig = field(
        default_factory=RuntimePolicyPromptActivationConfig
    )

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"enabled": self.enabled, "version": self.version}
        tool_policy: dict[str, object] = {}
        if self.tool_policy.default is not None:
            tool_policy["default"] = self.tool_policy.default
        if self.tool_policy.allowed:
            tool_policy["allow"] = list(self.tool_policy.allowed)
        if self.tool_policy.denied:
            tool_policy["deny"] = list(self.tool_policy.denied)
        if tool_policy:
            payload["tool_policy"] = tool_policy
        delegation_policy: dict[str, object] = {}
        if self.delegation_policy.default is not None:
            delegation_policy["default"] = self.delegation_policy.default
        if self.delegation_policy.allowed:
            delegation_policy["allow"] = list(self.delegation_policy.allowed)
        if self.delegation_policy.denied:
            delegation_policy["deny"] = list(self.delegation_policy.denied)
        if delegation_policy:
            payload["delegation_policy"] = delegation_policy
        hook_policy: dict[str, object] = {}
        if self.hook_policy.allowed_event_scopes:
            hook_policy["allowed_event_scopes"] = list(self.hook_policy.allowed_event_scopes)
        if self.hook_policy.actions:
            hook_policy["actions"] = list(self.hook_policy.actions)
        if hook_policy:
            payload["hook_policy"] = hook_policy
        prompt_activation: dict[str, object] = {
            "enabled": self.prompt_activation.enabled,
        }
        if self.prompt_activation.profile_refs:
            prompt_activation["profile_refs"] = list(self.prompt_activation.profile_refs)
        payload["prompt_activation"] = prompt_activation
        return payload


@dataclass(frozen=True, slots=True)
class RuntimePolicySnapshot:
    agent_preset: str
    agent_manifest_id: str
    intent: Mapping[str, object]
    tool_policy: Mapping[str, object]
    delegation_policy: Mapping[str, object]
    hook_policy: Mapping[str, object]
    prompt_activation: Mapping[str, object]
    precedence_trace: Sequence[Mapping[str, object]]
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    created_at: int | None = None
    mode: str = "normal"
    read_only: bool = False
    schema_version: int = POLICY_SCHEMA_VERSION
    policy_version: str = POLICY_VERSION

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "agent_preset": self.agent_preset,
            "agent_manifest_id": self.agent_manifest_id,
            "intent": dict(self.intent),
            "tool_policy": dict(self.tool_policy),
            "delegation_policy": dict(self.delegation_policy),
            "hook_policy": dict(self.hook_policy),
            "prompt_activation": dict(self.prompt_activation),
            "precedence_trace": [dict(entry) for entry in self.precedence_trace],
            "diagnostics": dict(self.diagnostics),
            "mode": self.mode,
            "read_only": self.read_only,
        }
        if self.created_at is not None:
            payload["created_at"] = self.created_at
        return payload


def runtime_policy_allowed_hook_scopes() -> tuple[str, ...]:
    return _ALLOWED_HOOK_SCOPES


def validate_runtime_policy_config_payload(
    raw_policy: object,
    *,
    source: str,
) -> RuntimePolicyConfig | None:
    if raw_policy is None:
        return None
    if not isinstance(raw_policy, dict):
        raise ValueError(f"{source} must be an object when provided")
    payload = cast(dict[str, object], raw_policy)
    allowed_keys = {
        "enabled",
        "version",
        "tool_policy",
        "delegation_policy",
        "hook_policy",
        "prompt_activation",
    }
    _reject_unknown_keys(payload, allowed_keys=allowed_keys, source=source)
    enabled = True
    tool_policy = RuntimePolicyToolPolicyConfig()
    delegation_policy = RuntimePolicyDelegationPolicyConfig()
    hook_policy = RuntimePolicyHookPolicyConfig()
    prompt_activation = RuntimePolicyPromptActivationConfig()
    if "enabled" in payload:
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(f"{source}.enabled must be a boolean")
        enabled = enabled
    version = payload.get("version", POLICY_VERSION)
    if version != POLICY_VERSION:
        raise ValueError(f"{source}.version must be {POLICY_VERSION!r}")
    if "tool_policy" in payload:
        tool_payload = _validate_policy_list_section(
            payload["tool_policy"],
            source=f"{source}.tool_policy",
            allowed_keys={"allow", "deny", "default"},
        )
        tool_policy = RuntimePolicyToolPolicyConfig(
            default=_string(tool_payload.get("default")),
            allowed=cast(tuple[str, ...], tool_payload.get("allow", ())),
            denied=cast(tuple[str, ...], tool_payload.get("deny", ())),
        )
    if "delegation_policy" in payload:
        delegation = _validate_policy_list_section(
            payload["delegation_policy"],
            source=f"{source}.delegation_policy",
            allowed_keys={"allow", "deny", "default"},
        )
        if "product" in cast(tuple[str, ...], delegation.get("allow", ())):
            raise ValueError(PRODUCT_DELEGATION_DENIAL_REASON)
        delegation_policy = RuntimePolicyDelegationPolicyConfig(
            default=_string(delegation.get("default")),
            allowed=cast(tuple[str, ...], delegation.get("allow", ())),
            denied=cast(tuple[str, ...], delegation.get("deny", ())),
        )
    if "hook_policy" in payload:
        raw_hook_policy = _validate_hook_policy_config(
            payload["hook_policy"],
            source=f"{source}.hook_policy",
        )
        hook_policy = RuntimePolicyHookPolicyConfig(
            allowed_event_scopes=cast(
                tuple[str, ...],
                raw_hook_policy.get("allowed_event_scopes", ()),
            ),
            actions=cast(tuple[str, ...], raw_hook_policy.get("actions", ())),
        )
    if "prompt_activation" in payload:
        raw_prompt_activation = _validate_prompt_activation_config(
            payload["prompt_activation"], source=f"{source}.prompt_activation"
        )
        prompt_activation = RuntimePolicyPromptActivationConfig(
            enabled=raw_prompt_activation.get("enabled", True) is not False,
            profile_refs=cast(tuple[str, ...], raw_prompt_activation.get("profile_refs", ())),
        )
    return RuntimePolicyConfig(
        version=POLICY_VERSION,
        enabled=enabled,
        tool_policy=tool_policy,
        delegation_policy=delegation_policy,
        hook_policy=hook_policy,
        prompt_activation=prompt_activation,
    )


def _clean_source(source: str) -> str:
    return source.replace("runtime config field ", "").replace("'", "")


def _reject_unknown_keys(
    payload: Mapping[str, object],
    *,
    allowed_keys: set[str],
    source: str,
) -> None:
    unknown = sorted(key for key in payload if key not in allowed_keys)
    if unknown:
        raise ValueError(f"{_clean_source(source)}.{unknown[0]} is not supported")


def _string_tuple(value: object, *, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ValueError(f"{source} must be an array of strings")
    parsed: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise ValueError(f"{source}[{index}] must be a non-empty string")
        parsed.append(item)
    return tuple(dict.fromkeys(parsed))


def _validate_policy_list_section(
    value: object,
    *,
    source: str,
    allowed_keys: set[str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{source} must be an object when provided")
    payload = cast(dict[str, object], value)
    _reject_unknown_keys(payload, allowed_keys=allowed_keys, source=source)
    normalized: dict[str, object] = {}
    for key in sorted(allowed_keys):
        if key not in payload:
            continue
        if key == "default":
            default = payload[key]
            if not isinstance(default, str) or not default:
                raise ValueError(f"{source}.default must be a non-empty string")
            normalized[key] = default
        else:
            normalized[key] = _string_tuple(payload[key], source=f"{source}.{key}")
    return normalized


def _validate_hook_policy_config(value: object, *, source: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{source} must be an object when provided")
    payload = cast(dict[str, object], value)
    _reject_unknown_keys(payload, allowed_keys={"allowed_event_scopes", "actions"}, source=source)
    normalized: dict[str, object] = {}
    if "allowed_event_scopes" in payload:
        scopes = _string_tuple(
            payload["allowed_event_scopes"],
            source=f"{source}.allowed_event_scopes",
        )
        unknown = [scope for scope in scopes if scope not in _ALLOWED_HOOK_SCOPES]
        if unknown:
            raise ValueError(
                f"{_clean_source(source)}.allowed_event_scopes contains "
                f"unsupported scope: {unknown[0]}"
            )
        normalized["allowed_event_scopes"] = scopes
    if "actions" in payload:
        actions = _string_tuple(payload["actions"], source=f"{source}.actions")
        normalized["actions"] = tuple(
            action for action in actions if action in _ALLOWED_HOOK_ACTIONS
        )
    return normalized


def _validate_prompt_activation_config(value: object, *, source: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{source} must be an object when provided")
    payload = cast(dict[str, object], value)
    _reject_unknown_keys(payload, allowed_keys={"enabled", "profile_refs"}, source=source)
    normalized: dict[str, object] = {}
    if "enabled" in payload:
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(f"{source}.enabled must be a boolean")
        normalized["enabled"] = enabled
    if "profile_refs" in payload:
        normalized["profile_refs"] = _string_tuple(
            payload["profile_refs"],
            source=f"{source}.profile_refs",
        )
    return normalized


def serialize_runtime_policy_config(config: object) -> dict[str, object] | None:
    if config is None:
        return None
    if isinstance(config, RuntimePolicyConfig):
        return config.as_payload()
    if isinstance(config, dict):
        return _json_safe_dict(cast(dict[str, object], config))
    return None


def materialize_runtime_policy_snapshot(**inputs: Any) -> RuntimePolicySnapshot:
    persisted = _existing_snapshot(inputs.get("persisted_session_policy"))
    if persisted is not None:
        return _snapshot_from_payload(persisted)

    parent_snapshot = _existing_snapshot(inputs.get("parent_snapshot"))
    if parent_snapshot is not None:
        parent_snapshot = _validated_snapshot_payload(parent_snapshot)

    runtime_config = _mapping(inputs.get("runtime_config"))
    policy_config = _mapping(runtime_config.get("policy"))
    request_metadata = _mapping(inputs.get("request_metadata"))
    agent_preset = (
        _string(inputs.get("agent_preset")) or _agent_preset_from_config(runtime_config) or "leader"
    )
    agent_manifest_id = _string(inputs.get("agent_manifest_id")) or agent_preset
    mode = _runtime_mode(request_metadata)
    read_only = _runtime_read_only(request_metadata, mode=mode)
    tool_allowed = _tool_allowed(runtime_config, policy_config)
    delegation_allowed = _delegation_allowed(policy_config)
    hook_policy_request = _mapping(inputs.get("hook_policy_request"))
    hook_actions = _hook_actions(policy_config, hook_policy_request)
    denied = [{"target": "product", "reason": PRODUCT_DELEGATION_DENIAL_REASON}]

    trace = _base_precedence_trace(synthesized=False)
    if _requested_product_delegation(request_metadata) or _policy_mentions_product(policy_config):
        trace[0]["reason"] = PRODUCT_DELEGATION_DENIAL_REASON
    trace[6] = _intent_metadata_trace()

    snapshot = RuntimePolicySnapshot(
        agent_preset=agent_preset,
        agent_manifest_id=agent_manifest_id,
        intent=_neutral_intent_payload(),
        tool_policy={
            "allowed": list(tool_allowed),
            "denied": [],
            "source": "runtime_config" if tool_allowed else "runtime_defaults",
        },
        delegation_policy={
            "allowed_presets": [preset for preset in delegation_allowed if preset != "product"],
            "denied": denied,
            "product_denial_reason": PRODUCT_DELEGATION_DENIAL_REASON,
        },
        hook_policy={
            "allowed_event_scopes": list(_hook_scopes(policy_config)),
            "actions": list(hook_actions),
            "authoritative": False,
        },
        prompt_activation={
            "enabled": _prompt_activation_enabled(policy_config),
            "raw_prompt_stored": False,
        },
        precedence_trace=trace,
        diagnostics=_diagnostics(inputs.get("diagnostics_input")),
        created_at=int(time.time() * 1000),
        mode=mode,
        read_only=read_only,
    )
    if parent_snapshot is None:
        return snapshot
    return _child_snapshot_from_parent(snapshot=snapshot, parent_snapshot=parent_snapshot)


def synthesize_legacy_runtime_policy_snapshot(**inputs: Any) -> RuntimePolicySnapshot:
    session_metadata = _mapping(inputs.get("session_metadata"))
    runtime_config = _mapping(session_metadata.get("runtime_config"))
    request_metadata = dict(session_metadata)
    agent_preset = (
        _agent_preset_from_config(runtime_config)
        or _agent_preset_from_config(session_metadata)
        or "leader"
    )
    mode = _runtime_mode(request_metadata)
    read_only = _runtime_read_only(request_metadata, mode=mode)
    trace = _base_precedence_trace(synthesized=True)
    trace.insert(
        0,
        {
            "source": "legacy_policy_synthesis",
            "applied": True,
            "reason": "missing_stored_runtime_policy_snapshot",
        },
    )
    return RuntimePolicySnapshot(
        agent_preset=agent_preset,
        agent_manifest_id=agent_preset,
        intent=_neutral_intent_payload(),
        tool_policy={"allowed": [], "denied": [], "source": "legacy_conservative_default"},
        delegation_policy={
            "allowed_presets": list(_ALLOWED_CHILD_PRESETS),
            "denied": [{"target": "product", "reason": PRODUCT_DELEGATION_DENIAL_REASON}],
            "product_denial_reason": PRODUCT_DELEGATION_DENIAL_REASON,
        },
        hook_policy={
            "allowed_event_scopes": list(_ALLOWED_HOOK_SCOPES),
            "actions": ["observe", "report"],
            "authoritative": False,
        },
        prompt_activation={"enabled": True, "raw_prompt_stored": False},
        precedence_trace=trace,
        diagnostics={"synthesized": True},
        created_at=int(time.time() * 1000),
        mode=mode,
        read_only=read_only,
    )


def runtime_policy_snapshot_from_session_metadata(metadata: dict[str, object]) -> dict[str, object]:
    existing = _existing_snapshot(metadata.get("runtime_policy"))
    if existing is not None:
        if _has_explicit_snapshot_version(existing):
            return _snapshot_from_payload(existing).as_payload()
        return synthesize_legacy_runtime_policy_snapshot(session_metadata=metadata).as_payload()
    return synthesize_legacy_runtime_policy_snapshot(session_metadata=metadata).as_payload()


def _snapshot_from_payload(payload: Mapping[str, object]) -> RuntimePolicySnapshot:
    payload = _validated_snapshot_payload(payload)
    return RuntimePolicySnapshot(
        agent_preset=_string(payload.get("agent_preset")) or "leader",
        agent_manifest_id=(
            _string(payload.get("agent_manifest_id"))
            or _string(payload.get("agent_preset"))
            or "leader"
        ),
        intent=_mapping(payload.get("intent")),
        tool_policy=_mapping(payload.get("tool_policy")),
        delegation_policy=_mapping(payload.get("delegation_policy")),
        hook_policy=_mapping(payload.get("hook_policy")),
        prompt_activation=_mapping(payload.get("prompt_activation")),
        precedence_trace=tuple(_trace_entries(payload.get("precedence_trace"))),
        diagnostics=_mapping(payload.get("diagnostics")),
        created_at=_int_or_none(payload.get("created_at")),
        mode=_string(payload.get("mode")) or "normal",
        read_only=payload.get("read_only") is True,
        schema_version=POLICY_SCHEMA_VERSION,
        policy_version=POLICY_VERSION,
    )


def _has_explicit_snapshot_version(payload: Mapping[str, object]) -> bool:
    return "schema_version" in payload or "policy_version" in payload


def _validated_snapshot_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    schema_version = payload.get("schema_version")
    policy_version = payload.get("policy_version")
    if schema_version != POLICY_SCHEMA_VERSION:
        raise RuntimePolicySnapshotVersionError(
            "unsupported runtime_policy schema_version: "
            f"{schema_version!r}; expected {POLICY_SCHEMA_VERSION!r}"
        )
    if policy_version != POLICY_VERSION:
        raise RuntimePolicySnapshotVersionError(
            "unsupported runtime_policy policy_version: "
            f"{policy_version!r}; expected {POLICY_VERSION!r}"
        )
    return payload


def _child_snapshot_from_parent(
    *,
    snapshot: RuntimePolicySnapshot,
    parent_snapshot: Mapping[str, object],
) -> RuntimePolicySnapshot:
    parent_tool_policy = _mapping(parent_snapshot.get("tool_policy"))
    parent_delegation_policy = _mapping(parent_snapshot.get("delegation_policy"))
    parent_hook_policy = _mapping(parent_snapshot.get("hook_policy"))
    parent_prompt_activation = _mapping(parent_snapshot.get("prompt_activation"))
    child_tool_policy = _mapping(snapshot.tool_policy)
    child_delegation_policy = _mapping(snapshot.delegation_policy)
    child_hook_policy = _mapping(snapshot.hook_policy)
    child_prompt_activation = _mapping(snapshot.prompt_activation)

    parent_tools = _string_tuple(
        parent_tool_policy.get("allowed"),
        source="parent.tool_policy.allowed",
    )
    child_tools = _string_tuple(
        child_tool_policy.get("allowed"),
        source="child.tool_policy.allowed",
    )
    parent_delegation = _string_tuple(
        parent_delegation_policy.get("allowed_presets"),
        source="parent.delegation_policy.allowed_presets",
    )
    child_delegation = _string_tuple(
        child_delegation_policy.get("allowed_presets"),
        source="child.delegation_policy.allowed_presets",
    )
    parent_scopes = _string_tuple(
        parent_hook_policy.get("allowed_event_scopes"),
        source="parent.hook_policy.allowed_event_scopes",
    )
    child_scopes = _string_tuple(
        child_hook_policy.get("allowed_event_scopes"),
        source="child.hook_policy.allowed_event_scopes",
    )
    parent_actions = _string_tuple(
        parent_hook_policy.get("actions"),
        source="parent.hook_policy.actions",
    )
    child_actions = _string_tuple(
        child_hook_policy.get("actions"),
        source="child.hook_policy.actions",
    )
    parent_allowed_tools = set(parent_tools)
    parent_allowed_delegation = set(parent_delegation)
    parent_allowed_scopes = set(parent_scopes)
    parent_allowed_actions = set(parent_actions)

    trace = [dict(entry) for entry in snapshot.precedence_trace]
    trace.append(
        {
            "source": "parent_runtime_policy_snapshot",
            "applied": True,
            "reason": "child_policy_subset_of_parent_capabilities",
        }
    )
    return RuntimePolicySnapshot(
        agent_preset=snapshot.agent_preset,
        agent_manifest_id=snapshot.agent_manifest_id,
        intent=snapshot.intent,
        tool_policy={
            **child_tool_policy,
            "allowed": [tool for tool in child_tools if tool in parent_allowed_tools],
            "source": "parent_runtime_policy_snapshot",
        },
        delegation_policy={
            **child_delegation_policy,
            "allowed_presets": [
                preset for preset in child_delegation if preset in parent_allowed_delegation
            ],
        },
        hook_policy={
            **child_hook_policy,
            "allowed_event_scopes": [
                scope for scope in child_scopes if scope in parent_allowed_scopes
            ],
            "actions": [action for action in child_actions if action in parent_allowed_actions],
        },
        prompt_activation={
            **child_prompt_activation,
            "enabled": parent_prompt_activation.get("enabled", True) is not False
            and child_prompt_activation.get("enabled", True) is not False,
        },
        precedence_trace=trace,
        diagnostics=snapshot.diagnostics,
        created_at=snapshot.created_at,
        mode=snapshot.mode,
        read_only=snapshot.read_only,
    )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mapping(value: object) -> dict[str, object]:
    return dict(cast(dict[str, object], value)) if isinstance(value, dict) else {}


def _existing_snapshot(value: object) -> Mapping[str, object] | None:
    if isinstance(value, RuntimePolicySnapshot):
        return value.as_payload()
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return None


def _trace_entries(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    return [_mapping(item) for item in value if isinstance(item, dict)]


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _neutral_intent_payload() -> dict[str, object]:
    return {
        "label": _INTENT_LABEL_UNSPECIFIED,
        "confidence": 0.0,
        "matched_rule_ids": [],
        "authoritative": False,
    }


def _intent_metadata_trace() -> dict[str, object]:
    return {
        "source": "intent_metadata",
        "applied": False,
        "authoritative": False,
        "label": _INTENT_LABEL_UNSPECIFIED,
        "confidence": 0.0,
        "matched_rule_ids": [],
        "reason": "neutral_bounded_metadata_only",
    }


def _runtime_mode(metadata: Mapping[str, object]) -> str:
    value = metadata.get("mode", "normal")
    return cast(str, value) if value in {"normal", "analyze", "plan"} else "normal"


def _runtime_read_only(metadata: Mapping[str, object], *, mode: str) -> bool:
    if mode in {"analyze", "plan"}:
        return True
    return metadata.get("read_only") is True


def _agent_preset_from_config(payload: Mapping[str, object]) -> str | None:
    agent = payload.get("agent")
    if isinstance(agent, dict):
        preset = cast(dict[str, object], agent).get("preset")
        if isinstance(preset, str) and preset:
            return preset
    return None


def _tool_allowed(
    runtime_config: Mapping[str, object],
    policy_config: Mapping[str, object],
) -> tuple[str, ...]:
    policy_tool = _mapping(policy_config.get("tool_policy"))
    policy_allowed = _string_tuple(policy_tool.get("allow"), source="policy.tool_policy.allow")
    tools = _mapping(runtime_config.get("tools"))
    config_allowed = _string_tuple(tools.get("allowlist"), source="tools.allowlist")
    return tuple(dict.fromkeys((*config_allowed, *policy_allowed)))


def _delegation_allowed(policy_config: Mapping[str, object]) -> tuple[str, ...]:
    delegation = _mapping(policy_config.get("delegation_policy"))
    allowed = _string_tuple(delegation.get("allow"), source="policy.delegation_policy.allow")
    if not allowed:
        allowed = _ALLOWED_CHILD_PRESETS
    return tuple(item for item in allowed if item in (*_ALLOWED_CHILD_PRESETS, "product"))


def _hook_actions(
    policy_config: Mapping[str, object],
    request: Mapping[str, object],
) -> tuple[str, ...]:
    hook_policy = _mapping(policy_config.get("hook_policy"))
    actions = _string_tuple(hook_policy.get("actions"), source="policy.hook_policy.actions")
    requested = _string_tuple(request.get("actions"), source="hook_policy_request.actions")
    filtered = tuple(action for action in (*actions, *requested) if action in _ALLOWED_HOOK_ACTIONS)
    return tuple(dict.fromkeys(filtered or ("observe", "report")))


def _hook_scopes(policy_config: Mapping[str, object]) -> tuple[str, ...]:
    hook_policy = _mapping(policy_config.get("hook_policy"))
    scopes = _string_tuple(
        hook_policy.get("allowed_event_scopes"),
        source="policy.hook_policy.allowed_event_scopes",
    )
    if not scopes:
        return _ALLOWED_HOOK_SCOPES
    return tuple(scope for scope in scopes if scope in _ALLOWED_HOOK_SCOPES)


def _prompt_activation_enabled(policy_config: Mapping[str, object]) -> bool:
    prompt_activation = _mapping(policy_config.get("prompt_activation"))
    return prompt_activation.get("enabled", True) is not False


def _requested_product_delegation(metadata: Mapping[str, object]) -> bool:
    delegation = metadata.get("delegation")
    if not isinstance(delegation, dict):
        return False
    payload = cast(dict[str, object], delegation)
    return payload.get("subagent_type") == "product" or payload.get("selected_preset") == "product"


def _policy_mentions_product(policy_config: Mapping[str, object]) -> bool:
    delegation = _mapping(policy_config.get("delegation_policy"))
    return "product" in _string_tuple(
        delegation.get("allow"),
        source="policy.delegation_policy.allow",
    )


def _base_precedence_trace(*, synthesized: bool) -> list[dict[str, object]]:
    return [
        {"source": "runtime_hard_denials", "applied": True},
        {"source": "persisted_session_policy", "applied": not synthesized},
        {"source": "runtime_config", "applied": True},
        {"source": "agent_manifest", "applied": True},
        {"source": "request_session_options", "applied": True},
        {"source": "hook_preset_metadata", "applied": True},
        {"source": "intent_metadata", "applied": False, "authoritative": False},
        {"source": "runtime_defaults", "applied": True},
    ]


def _looks_sensitive_key(key: str) -> bool:
    return any(fragment in key.lower() for fragment in _SECRET_KEY_FRAGMENTS)


def _diagnostics(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    diagnostics: dict[str, object] = {}
    for index, (raw_key, raw_item) in enumerate(cast(dict[object, object], value).items()):
        if index >= _DIAGNOSTIC_LIMIT:
            diagnostics["truncated"] = True
            break
        key = str(raw_key)
        if _looks_sensitive_key(key):
            diagnostics[key] = "<redacted>"
        elif isinstance(raw_item, str):
            diagnostics[key] = raw_item[:256]
        elif isinstance(raw_item, bool | int | float) or raw_item is None:
            diagnostics[key] = raw_item
        else:
            diagnostics[key] = "<bounded>"
    diagnostics.pop("raw_prompt", None)
    diagnostics.pop("raw_skill_body", None)
    return diagnostics


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item) for key, item in cast(dict[object, object], value).items()
        }
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _json_safe_dict(value: Mapping[str, object]) -> dict[str, object]:
    return cast(dict[str, object], _json_safe(dict(value)))
