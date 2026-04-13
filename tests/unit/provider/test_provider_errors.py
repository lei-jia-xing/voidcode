from __future__ import annotations

from voidcode.provider.errors import (
    SingleAgentContextLimitError,
    classify_provider_error,
    format_fallback_exhausted_error,
    format_invalid_provider_config_error,
)


def test_classify_provider_error_returns_context_limit_error_for_limit_messages() -> None:
    classified = classify_provider_error(ValueError("context window exceeded for provider"))

    assert isinstance(classified, SingleAgentContextLimitError)
    assert str(classified) == "context window exceeded for provider"


def test_classify_provider_error_returns_none_for_unrelated_errors() -> None:
    assert classify_provider_error(ValueError("network timeout")) is None


def test_format_invalid_provider_config_error_includes_field_and_reason() -> None:
    assert format_invalid_provider_config_error(
        "provider_fallback.preferred_model",
        "must match model when both are configured",
    ) == (
        "invalid provider config: provider_fallback.preferred_model "
        "must match model when both are configured"
    )


def test_format_fallback_exhausted_error_includes_provider_model_and_attempt() -> None:
    assert (
        format_fallback_exhausted_error(
            provider_name="opencode",
            model_name="gpt-5.4",
            attempt=2,
        )
        == "provider fallback exhausted after opencode/gpt-5.4 failed at attempt 2"
    )
