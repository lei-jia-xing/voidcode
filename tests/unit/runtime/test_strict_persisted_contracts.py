from __future__ import annotations

from typing import cast

import pytest

from voidcode.provider.protocol import ProviderTokenUsage
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.config_materializer import (
    PERSISTED_RUNTIME_CONFIG_KEYS,
    EffectiveRuntimeConfig,
    parse_persisted_runtime_config,
    serialize_runtime_config_core,
)
from voidcode.runtime.execution.provider_execution_metadata import (
    provider_attempt_from_metadata,
    provider_retry_attempt_from_metadata,
    session_with_provider_usage_metadata,
)
from voidcode.runtime.session import SessionRef, SessionState
from voidcode.runtime.session_metadata_helpers import plan_state_from_metadata


def _current_runtime_config_payload() -> dict[str, object]:
    defaults = RuntimeConfig()
    return serialize_runtime_config_core(
        EffectiveRuntimeConfig(
            approval_mode=defaults.approval_mode,
            permission=defaults.permission,
            model=defaults.model,
            execution_engine=defaults.execution_engine,
            tool_timeout_seconds=defaults.tool_timeout_seconds,
        )
    )


@pytest.mark.parametrize(
    "field",
    (
        "approval_mode",
        "permission",
        "execution_engine",
        "tool_timeout_seconds",
        "fallback_models",
    ),
)
def test_persisted_runtime_config_rejects_missing_current_fields(field: str) -> None:
    payload = _current_runtime_config_payload()
    del payload[field]

    with pytest.raises(ValueError, match="missing required field"):
        parse_persisted_runtime_config(payload)


def test_persisted_permission_rejects_missing_current_scope() -> None:
    payload = _current_runtime_config_payload()
    permission = dict(cast(dict[str, object], payload["permission"]))
    del permission["external_directory_write"]
    payload["permission"] = permission

    with pytest.raises(ValueError, match="permission is missing required field"):
        parse_persisted_runtime_config(payload)


@pytest.mark.parametrize("field", ("provider_attempt", "provider_retry_attempt"))
def test_provider_attempt_metadata_rejects_invalid_persisted_values(field: str) -> None:
    parser = provider_attempt_from_metadata if field == "provider_attempt" else provider_retry_attempt_from_metadata
    with pytest.raises(ValueError, match=field):
        parser({field: "0"})


def test_provider_usage_rejects_malformed_persisted_cumulative_state() -> None:
    session = SessionState(
        session=SessionRef(id="strict-provider-usage"),
        metadata={"provider_usage": {"cumulative": [], "turn_count": 1}},
    )

    with pytest.raises(ValueError, match="provider_usage.cumulative"):
        session_with_provider_usage_metadata(
            session,
            ProviderTokenUsage(input_tokens=1, output_tokens=1),
        )


def test_plan_state_rejects_malformed_persisted_value() -> None:
    with pytest.raises(ValueError, match="persisted plan_state"):
        plan_state_from_metadata({"plan_state": []})


_EXPECTED_PERSISTED_RUNTIME_CONFIG_KEYS = {
    "approval_mode",
    "permission",
    "policy",
    "execution_engine",
    "tool_timeout_seconds",
    "reasoning_effort",
    "model",
    "fallback_models",
    "providers",
    "resolved_provider",
    "resolved_hook_presets",
    "tools",
    "agent",
    "agents",
    "context_window",
    "lsp",
    "mcp",
}


def _accepted_persisted_runtime_config_values() -> dict[str, object]:
    return {
        "approval_mode": "deny",
        "permission": {
            "external_directory_read": {"*": "deny"},
            "external_directory_write": {"*": "ask"},
        },
        "policy": {
            "enabled": True,
            "version": "v1",
            "prompt_activation": {"enabled": True},
        },
        "execution_engine": "deterministic",
        "tool_timeout_seconds": None,
        "reasoning_effort": "medium",
        "model": "provider/model",
        "fallback_models": [],
        "providers": {
            "custom": {
                "local": {
                    "transient_retry": {"max_retries": 1},
                },
            },
        },
        "resolved_provider": {
            "active_target": {
                "raw_model": "provider/model",
                "provider": "provider",
                "model": "model",
            },
            "targets": [
                {
                    "raw_model": "provider/model",
                    "provider": "provider",
                    "model": "model",
                },
            ],
        },
        "resolved_hook_presets": {"refs": []},
        "tools": {"builtin": {"enabled": True}, "allowlist": ["read"]},
        "agent": {"preset": "leader"},
        "agents": {"leader": {"preset": "leader"}},
        "context_window": {"auto_compaction": False},
        "lsp": {"mode": "disabled", "configured_enabled": False, "servers": []},
        "mcp": {"mode": "managed", "configured_enabled": False, "servers": []},
    }


def test_persisted_runtime_config_key_set_is_current_and_complete() -> None:
    assert set(PERSISTED_RUNTIME_CONFIG_KEYS) == _EXPECTED_PERSISTED_RUNTIME_CONFIG_KEYS


@pytest.mark.parametrize("field", sorted(_EXPECTED_PERSISTED_RUNTIME_CONFIG_KEYS))
def test_persisted_runtime_config_accepts_representative_value_for_each_key(field: str) -> None:
    payload = _current_runtime_config_payload()
    payload[field] = _accepted_persisted_runtime_config_values()[field]

    materialized = parse_persisted_runtime_config(payload)

    if field == "approval_mode":
        assert materialized.approval_mode == "deny"
    elif field == "permission":
        assert materialized.permission.read.rules == (("*", "deny"),)
        assert materialized.permission.write.rules == (("*", "ask"),)
    elif field == "policy":
        assert materialized.policy is not None
        assert materialized.policy.version == "v1"
    elif field == "execution_engine":
        assert materialized.execution_engine == "deterministic"
    elif field == "tool_timeout_seconds":
        assert materialized.tool_timeout_seconds is None
    elif field == "reasoning_effort":
        assert materialized.reasoning_effort == "medium"
    elif field == "model":
        assert materialized.model == "provider/model"
    elif field == "fallback_models":
        assert materialized.provider_fallback is None
    elif field == "providers":
        assert materialized.providers is not None
    elif field == "tools":
        assert materialized.tools is not None
        assert materialized.tools.allowlist == ("read",)
    elif field == "context_window":
        assert materialized.context_window is not None
        assert materialized.context_window.auto_compaction is False
    elif field == "agent":
        assert materialized.has_agent is True
        assert materialized.raw_agent == {"preset": "leader"}
    elif field == "resolved_provider":
        assert materialized.raw_resolved_provider == _accepted_persisted_runtime_config_values()[field]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("approval_mode", "invalid", "persisted runtime_config approval_mode is invalid"),
        (
            "permission",
            {},
            "persisted runtime_config permission is missing required field\\(s\\): external_directory_read, external_directory_write",
        ),
        ("policy", [], "persisted runtime_config.policy must be an object when provided"),
        ("execution_engine", "invalid", "persisted runtime_config execution_engine is invalid"),
        ("tool_timeout_seconds", 0, "persisted runtime_config tool_timeout_seconds must be at least 1"),
        ("reasoning_effort", "invalid", "reasoning_effort must be one of:"),
        ("model", 7, "persisted runtime_config model must be a string or null"),
        ("fallback_models", [7], "invalid provider config"),
        ("providers", [], "invalid provider config"),
        ("tools", [], "invalid provider config"),
        ("context_window", [], "invalid provider config"),
    ),
)
def test_persisted_runtime_config_preserves_rejection_semantics(
    field: str,
    value: object,
    error: str,
) -> None:
    payload = _current_runtime_config_payload()
    payload[field] = value
    if field == "fallback_models":
        payload["model"] = "provider/model"

    with pytest.raises(ValueError, match=error):
        parse_persisted_runtime_config(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("resolved_provider", "not-materialized-by-parser"),
        ("resolved_hook_presets", []),
        ("agent", []),
        ("agents", []),
        ("lsp", 7),
        ("mcp", "not-materialized-by-parser"),
    ),
)
def test_persisted_snapshot_keys_remain_opaque_to_materializer(field: str, value: object) -> None:
    payload = _current_runtime_config_payload()
    payload[field] = value

    materialized = parse_persisted_runtime_config(payload)

    if field == "agent":
        assert materialized.has_agent is True
        assert materialized.raw_agent == value
    elif field == "resolved_provider":
        assert materialized.raw_resolved_provider == value
    else:
        assert materialized.has_agent is False
        assert materialized.raw_agent is None
        assert materialized.raw_resolved_provider is None
