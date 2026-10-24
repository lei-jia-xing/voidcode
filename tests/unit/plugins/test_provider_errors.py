"""Retry scheduling follows structured provider hints, not guessed prose."""

import pytest

from voidcode.capabilities.providers.native.errors import parse_provider_error


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", True])
def test_invalid_retry_delays_are_ignored(value):
    assert parse_provider_error({"status_code": 429, "retry_after": value}).retry_after is None
    assert parse_provider_error({"status_code": 429, "headers": {"Retry-After": value}}).retry_after is None


def test_structured_delays_preserve_zero_and_choose_longest():
    assert parse_provider_error({"retry_after": 0}).retry_after == 0
    error = parse_provider_error({"status_code": 429, "headers": {"Retry-After": "2", "retry-after-ms": "5000"}})
    assert error.retry_after == 5
    assert error.kind == "rate_limit"
    assert parse_provider_error({"retry_after": 7200}).retry_after == 3600


def test_error_prose_is_not_a_retry_schedule():
    error = parse_provider_error({"status_code": 429, "message": "Try again in 2 hours; reset at 2099-01-01 12:00:00"})
    assert error.retry_after is None
    assert "Try again" in error.message


def test_google_stream_error_keeps_stream_identity():
    from google.genai.errors import APIError

    from voidcode.capabilities.providers.native.google_native import GoogleGenAIProvider

    error = GoogleGenAIProvider._map_exception(
        APIError(429, {"error": {"message": "rate limited"}}), provider_name="google", model_name="model", source="stream"
    )
    assert error.kind == "rate_limit"
    assert error.details["source"] == "stream"
    assert error.message == "rate limited"


def test_unsupported_retry_configuration_is_rejected():
    from voidcode.capabilities.providers.dispatch import provider_configs

    with pytest.raises(ValueError, match="transient_retry"):
        provider_configs({"providers": {"openai": {"transient_retry": {"max_retries": 3}}}})


@pytest.mark.parametrize("field", ["refresh_token", "refresh_leeway_seconds"])
def test_unimplemented_token_refresh_configuration_is_rejected(field):
    from voidcode.capabilities.providers.dispatch import provider_configs

    with pytest.raises(ValueError, match=field):
        provider_configs({"providers": {"github-copilot": {"auth": {"method": "oauth", "token": "supplied-token", field: "unused"}}}})


def test_copilot_environment_token_resolves_without_an_ambiguous_reference():
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload
    from voidcode.capabilities.providers.native.provider_table import PROVIDER_TABLE_BY_ID

    name = PROVIDER_TABLE_BY_ID["github-copilot"].env_vars[0]
    configs = parse_provider_configs_payload({"github-copilot": {"auth": {"method": "token"}}}, source="test", env={name: "test-token"})
    assert configs.github_copilot.auth.token == "test-token"
    assert configs.github_copilot.auth.token_env_var is None
