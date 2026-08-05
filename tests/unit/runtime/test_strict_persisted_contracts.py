from __future__ import annotations

from typing import cast

import pytest

from voidcode.provider.protocol import ProviderTokenUsage
from voidcode.runtime.config import RuntimeConfig
from voidcode.runtime.config_materializer import (
    EffectiveRuntimeConfig,
    parse_persisted_runtime_config,
    serialize_runtime_config_core,
)
from voidcode.runtime.provider_execution_metadata import (
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
            max_steps=defaults.max_steps,
            tool_timeout_seconds=defaults.tool_timeout_seconds,
        )
    )


@pytest.mark.parametrize(
    "field",
    (
        "approval_mode",
        "permission",
        "execution_engine",
        "max_steps",
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
    parser = (
        provider_attempt_from_metadata
        if field == "provider_attempt"
        else provider_retry_attempt_from_metadata
    )
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
