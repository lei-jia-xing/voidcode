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


@pytest.mark.parametrize("value", [0, -1, True, "30", float("nan"), float("inf"), float("-inf")])
def test_provider_timeout_requires_a_finite_positive_number(value):
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    with pytest.raises(ValueError, match="timeout_seconds"):
        parse_provider_configs_payload({"openai": {"timeout_seconds": value}}, source="test", env={})


@pytest.mark.parametrize("value", [None, 1, 0.25])
def test_provider_timeout_accepts_null_and_positive_numbers(value):
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    configs = parse_provider_configs_payload({"openai": {"timeout_seconds": value}}, source="test", env={})
    assert configs.openai.timeout_seconds == value


@pytest.mark.parametrize(("field", "value"), [("api_key", 123), ("base_url", False), ("ssl_verify", "false"), ("ssl_verify", 1)])
def test_provider_config_does_not_coerce_strings_or_booleans(field, value):
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    with pytest.raises(ValueError, match=field):
        parse_provider_configs_payload({"endpoint": {field: value}}, source="test", env={})


@pytest.mark.parametrize(
    "auth",
    [
        {"method": "oauth", "access_token": "supplied-token"},
        {"method": "service_account", "service_account_json_path": "account.json"},
    ],
)
def test_google_non_key_auth_ignores_ambient_api_key(auth):
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload
    from voidcode.capabilities.providers.native.provider_table import PROVIDER_TABLE_BY_ID

    name = PROVIDER_TABLE_BY_ID["google"].env_vars[0]
    configs = parse_provider_configs_payload({"google": {"auth": auth}}, source="test", env={name: "ambient-key"})
    assert configs.google.auth.method == auth["method"]
    assert configs.google.auth.api_key is None


def test_google_oauth_still_rejects_an_explicit_api_key():
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    with pytest.raises(ValueError, match="api_key.*must not be set"):
        parse_provider_configs_payload(
            {"google": {"auth": {"method": "oauth", "access_token": "token", "api_key": "explicit-key"}}}, source="test", env={}
        )


def test_provider_config_representations_omit_credentials():
    from voidcode.capabilities.providers.native.config import ProviderConfigsPayload, parse_provider_configs_payload

    secret = "credential-sentinel-do-not-print"
    raw = {
        "openai": {"api_key": secret},
        "anthropic": {"api_key": secret},
        "google": {"auth": {"method": "oauth", "access_token": secret}},
        "github-copilot": {"auth": {"method": "token", "token": secret}},
        "endpoint": {"api_key": secret},
        "deepseek": {"api_key": secret},
        "custom": {"gateway": {"api_key": secret}},
    }
    payload = ProviderConfigsPayload.model_validate(raw)
    configs = parse_provider_configs_payload(raw, source="test", env={})
    assert secret not in repr(payload)
    assert secret not in repr(configs)
    assert configs.openai.api_key == secret
    assert configs.google.auth.access_token == secret
    assert configs.github_copilot.auth.token == secret


def test_invalid_provider_config_traceback_omits_credential_input():
    import traceback

    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    secret = "credential-sentinel-do-not-print"
    with pytest.raises(ValueError) as caught:
        parse_provider_configs_payload({"openai": {"api_key": {"unexpected": secret}}}, source="test", env={})
    assert secret not in "".join(traceback.format_exception(caught.value))


def test_zen_google_route_preserves_configured_timeout():
    from voidcode.capabilities.providers.native.config import ProviderEndpointConfig
    from voidcode.capabilities.providers.native.model_routing import ModelRoute
    from voidcode.capabilities.providers.native.opencode import OpenCodeZenModelProvider

    gateway = OpenCodeZenModelProvider(config=ProviderEndpointConfig(api_key="gateway-key", timeout_seconds=17))
    provider = gateway._wire("model", ModelRoute(wire="google-generative-ai"))
    assert provider.config.timeout_seconds == 17
    assert provider.config.auth.api_key == "gateway-key"
