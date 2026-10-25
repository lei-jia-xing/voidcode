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
    from voidcode.capabilities.providers.native.opencode import OpenCodeZenModelProvider

    gateway = OpenCodeZenModelProvider(config=ProviderEndpointConfig(api_key="gateway-key", timeout_seconds=17))
    provider = gateway._wire("google-generative-ai")
    assert provider.config.timeout_seconds == 17
    assert provider.config.auth.api_key == "gateway-key"


@pytest.mark.parametrize("providers", [[], "invalid", {1: {}}, {" ": {}}, {"openai": []}, {"gateway": None}])
def test_registered_provider_ingress_rejects_malformed_shapes(providers):
    from voidcode.capabilities.providers.dispatch import build_registered_providers

    with pytest.raises(ValueError):
        build_registered_providers({"providers": providers})


@pytest.mark.parametrize("name", ["openai", "custom-gateway"])
def test_registered_provider_ingress_rejects_case_collisions(name):
    from voidcode.capabilities.providers.dispatch import build_registered_providers

    with pytest.raises(ValueError, match="duplicate provider id"):
        build_registered_providers({"providers": {name: {"api_key": "first"}, f" {name.upper()} ": {"api_key": "second"}}})


@pytest.mark.parametrize(
    ("name", "auth"),
    [("google", {"method": "oauth", "access_token": "supplied-token"}), ("github-copilot", {"method": "token", "token": "supplied-token"})],
)
def test_nested_auth_accepts_the_empty_flat_key_injected_by_core(name, auth):
    from voidcode.capabilities.providers.dispatch import provider_configs

    configs = provider_configs({"providers": {name: {"api_key": None, "auth": auth}}})
    assert configs.entry(name).auth.method == auth["method"]


@pytest.mark.parametrize("name", ["google", "github-copilot"])
def test_nested_auth_rejects_a_conflicting_flat_key(name):
    from voidcode.capabilities.providers.dispatch import provider_configs

    with pytest.raises(ValueError, match="must not combine api_key with explicit auth"):
        provider_configs({"providers": {name: {"api_key": "flat-key", "auth": {"method": "oauth"}}}})


@pytest.mark.parametrize(
    ("provider", "field", "value"),
    [
        ("anthropic", "beta_headers", "header"),
        ("anthropic", "beta_headers", [123]),
        ("endpoint", "model_map", {"": "model"}),
        ("endpoint", "model_map", {"alias": ""}),
        ("endpoint", "model_map", {1: "model"}),
        ("endpoint", "model_map", {"alias": 123}),
        ("endpoint", "model_map", ["model"]),
        ("endpoint", "auth_scheme", "unknown"),
        ("google", "auth", {"method": "unknown"}),
        ("github-copilot", "auth", {"method": True}),
    ],
)
def test_provider_collection_and_literal_constraints_reject_invalid_values(provider, field, value):
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    with pytest.raises(ValueError, match=field):
        parse_provider_configs_payload({provider: {field: value}}, source="test", env={})


def test_provider_collection_nulls_and_valid_values_have_one_typed_result():
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    empty = parse_provider_configs_payload({"anthropic": {"beta_headers": None}, "endpoint": {"model_map": None, "auth_scheme": None}}, source="test")
    assert empty.anthropic.beta_headers == ()
    assert empty.endpoint.model_map == {}
    assert empty.endpoint.auth_scheme == "bearer"
    configured = parse_provider_configs_payload(
        {"anthropic": {"beta_headers": ["feature-v1"]}, "endpoint": {"model_map": {"alias": "Case-Preserved"}, "auth_scheme": "none"}}, source="test"
    )
    assert configured.anthropic.beta_headers == ("feature-v1",)
    assert configured.endpoint.model_map == {"alias": "Case-Preserved"}
    assert configured.endpoint.auth_scheme == "none"


@pytest.mark.parametrize(
    ("method", "required"), [("api_key", "api_key"), ("oauth", "access_token"), ("service_account", "service_account_json_path")]
)
def test_google_auth_requires_exactly_the_selected_credential(method, required):
    from voidcode.capabilities.providers.native.config import parse_provider_configs_payload

    def parse(auth):
        return parse_provider_configs_payload({"google": {"auth": auth}}, source="test", env={})

    with pytest.raises(ValueError, match=f"{required}.*must be provided"):
        parse({"method": method})
    parsed = parse({"method": method, required: "configured-value"})
    assert getattr(parsed.google.auth, required) == "configured-value"
    for other in {"api_key", "access_token", "service_account_json_path"} - {required}:
        with pytest.raises(ValueError, match=f"{other}.*must not be set"):
            parse({"method": method, required: "configured-value", other: "conflicting-value"})


def test_openai_rejects_anthropic_cache_control_at_request_boundary():
    from voidcode.capabilities.providers.native.openai_native import OpenAIChatCompletionsProvider
    from voidcode.sdk.provider import ProviderExecutionError, ProviderTurnRequest

    with pytest.raises(ProviderExecutionError) as caught:
        OpenAIChatCompletionsProvider()._payload(ProviderTurnRequest(messages=(), model_name="model", cache_retention="long"), stream=False)
    assert caught.value.kind == "unsupported_feature"
    assert caught.value.details["reason"] == "unsupported_cache_retention"
