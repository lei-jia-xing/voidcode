from __future__ import annotations

import pytest

from voidcode.runtime.provider_inspection import (
    ProviderReadinessFacts,
    RuntimeProviderReadinessProjector,
)


@pytest.mark.parametrize(
    ("facts", "expected_status", "expected_ok"),
    [
        (
            ProviderReadinessFacts(provider="openai", model="gpt-4o", configured=True, auth_present=True),
            "ready",
            True,
        ),
        (
            ProviderReadinessFacts(provider=None, model=None, configured=False, auth_present=None),
            "missing_model",
            False,
        ),
        (
            ProviderReadinessFacts(
                provider="openai",
                model="unknown",
                configured=True,
                auth_present=False,
                auth_failure_kind="invalid_model",
            ),
            "invalid_model",
            False,
        ),
        (
            ProviderReadinessFacts(provider="custom", model="model-a", configured=False, auth_present=None),
            "unconfigured",
            False,
        ),
        (
            ProviderReadinessFacts(provider="openai", model="gpt-4o", configured=True, auth_present=False),
            "missing_auth",
            False,
        ),
        (
            ProviderReadinessFacts(
                provider="openai",
                model="gpt-4o",
                configured=True,
                auth_present=True,
                streaming_supported=False,
            ),
            "streaming_unsupported",
            False,
        ),
    ],
)
def test_provider_readiness_projector_covers_decision_precedence(
    facts: ProviderReadinessFacts,
    expected_status: str,
    expected_ok: bool,
) -> None:
    result = RuntimeProviderReadinessProjector.project(facts)

    assert result.status == expected_status
    assert result.ok is expected_ok


def test_provider_readiness_projector_preserves_resolved_diagnostics() -> None:
    facts = ProviderReadinessFacts(
        provider="openai",
        model="gpt-4o",
        configured=True,
        auth_present=True,
        streaming_configured=True,
        streaming_supported=True,
        context_window=128_000,
        max_output_tokens=16_384,
        fallback_chain=("openai/gpt-4o", "anthropic/claude"),
        reasoning_controls={"status": "forwarded"},
    )

    result = RuntimeProviderReadinessProjector.project(facts)

    assert result.streaming_configured is True
    assert result.streaming_supported is True
    assert result.context_window == 128_000
    assert result.max_output_tokens == 16_384
    assert result.fallback_chain == ("openai/gpt-4o", "anthropic/claude")
    assert result.reasoning_controls == {"status": "forwarded"}


def test_provider_readiness_projector_prefers_auth_message() -> None:
    result = RuntimeProviderReadinessProjector.project(
        ProviderReadinessFacts(
            provider="openai",
            model="gpt-4o",
            configured=True,
            auth_present=False,
            auth_failure_kind="missing_auth",
            auth_message="custom auth guidance",
        )
    )

    assert result.guidance == "custom auth guidance"


def test_missing_model_guidance_names_the_config_file_and_key() -> None:
    result = RuntimeProviderReadinessProjector.project(ProviderReadinessFacts(provider=None, model=None, configured=False, auth_present=None))

    assert result.status == "missing_model"
    assert ".voidcode.json" in result.guidance
    assert '"model": "<provider>/<model>"' in result.guidance
    assert "VOIDCODE_MODEL" in result.guidance


def test_unconfigured_guidance_names_the_provider_credentials_path() -> None:
    result = RuntimeProviderReadinessProjector.project(ProviderReadinessFacts(provider="openai", model="gpt-4o", configured=False, auth_present=None))

    assert result.status == "unconfigured"
    assert "providers.openai.api_key" in result.guidance
    assert ".voidcode.json" in result.guidance


def test_unconfigured_guidance_names_custom_provider_credentials_path() -> None:
    result = RuntimeProviderReadinessProjector.project(
        ProviderReadinessFacts(provider="my-gateway", model="model-a", configured=False, auth_present=None)
    )

    assert result.guidance.startswith("Provider 'my-gateway' has no credentials.")
    assert "providers.custom.my-gateway.api_key" in result.guidance


def test_missing_auth_guidance_names_the_provider_credentials_path() -> None:
    result = RuntimeProviderReadinessProjector.project(
        ProviderReadinessFacts(provider="anthropic", model="claude", configured=True, auth_present=False)
    )

    assert result.status == "missing_auth"
    assert "providers.anthropic.api_key" in result.guidance
