from __future__ import annotations

import pytest

from voidcode.runtime.contracts import ProviderModelsResult
from voidcode.runtime.provider_inspection import (
    ProviderValidationFacts,
    RuntimeProviderValidationProjector,
)


def test_provider_validation_projector_reports_unconfigured_catalog_facts() -> None:
    result = RuntimeProviderValidationProjector.project(
        ProviderValidationFacts(
            provider="custom",
            configured=False,
            auth_present=None,
            models=ProviderModelsResult(
                provider="custom",
                configured=False,
                source="fallback",
                last_error="not configured",
                discovery_mode="disabled",
            ),
        )
    )

    assert result.status == "unconfigured"
    assert result.failure_kind == "missing_auth"
    assert result.source == "fallback"
    assert result.last_error == "not configured"
    assert result.discovery_mode == "disabled"


def test_provider_validation_projector_reports_auth_failure_without_discovery() -> None:
    result = RuntimeProviderValidationProjector.project(
        ProviderValidationFacts(
            provider="openai",
            configured=True,
            auth_present=False,
            auth_failure_kind="missing_auth",
            auth_message="missing key",
        )
    )

    assert result.ok is False
    assert result.status == "missing_auth"
    assert result.message == "missing key"
    assert result.guidance == "missing key"


def test_provider_validation_projector_reports_discovery_failure() -> None:
    result = RuntimeProviderValidationProjector.project(
        ProviderValidationFacts(
            provider="openai",
            configured=True,
            auth_present=True,
            models=ProviderModelsResult(
                provider="openai",
                configured=True,
                last_refresh_status="failed",
                last_error="network unavailable",
                discovery_mode="configured_endpoint",
            ),
        )
    )

    assert result.ok is False
    assert result.status == "failed"
    assert result.failure_kind == "transient_failure"
    assert result.message == "network unavailable"


@pytest.mark.parametrize(
    ("refresh_status", "expected_ok", "expected_message"),
    [
        ("ok", True, "Remote provider validation succeeded."),
        (
            "skipped",
            False,
            "Provider credentials are configured; remote validation is unavailable.",
        ),
        (None, True, "Remote provider validation succeeded."),
    ],
)
def test_provider_validation_projector_maps_discovery_status(
    refresh_status: str | None,
    expected_ok: bool,
    expected_message: str,
) -> None:
    result = RuntimeProviderValidationProjector.project(
        ProviderValidationFacts(
            provider="openai",
            configured=True,
            auth_present=True,
            models=ProviderModelsResult(
                provider="openai",
                configured=True,
                last_refresh_status=refresh_status,
            ),
        )
    )

    assert result.ok is expected_ok
    assert result.status == (refresh_status or "ok")
    assert result.message == expected_message


def test_provider_validation_projector_requires_discovery_after_auth() -> None:
    with pytest.raises(ValueError, match="requires model discovery facts"):
        RuntimeProviderValidationProjector.project(
            ProviderValidationFacts(
                provider="openai",
                configured=True,
                auth_present=True,
            )
        )
