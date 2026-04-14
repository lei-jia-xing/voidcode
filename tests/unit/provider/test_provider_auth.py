from __future__ import annotations

import pytest

from voidcode.provider.auth import (
    ProviderAuthAuthorizeRequest,
    ProviderAuthCallbackRequest,
    ProviderAuthResolutionError,
    ProviderAuthResolver,
    provider_auth_error_to_execution_kind,
)
from voidcode.provider.config import (
    CopilotProviderAuthConfig,
    CopilotProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
)


def test_provider_auth_methods_discovery_defaults_for_all_target_providers() -> None:
    resolver = ProviderAuthResolver(providers=ProviderConfigs())

    assert resolver.methods("openai").default_method == "api_key"
    assert [method.id for method in resolver.methods("openai").methods] == ["api_key"]
    assert resolver.methods("anthropic").default_method == "api_key"
    assert [method.id for method in resolver.methods("anthropic").methods] == ["api_key"]
    assert resolver.methods("google").default_method == "api_key"
    assert [method.id for method in resolver.methods("google").methods] == [
        "api_key",
        "oauth",
        "service_account",
    ]
    assert resolver.methods("copilot").default_method == "token"
    assert [method.id for method in resolver.methods("copilot").methods] == ["token", "oauth"]


def test_provider_auth_methods_discovery_uses_configured_defaults() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="service_account")),
            copilot=CopilotProviderConfig(auth=CopilotProviderAuthConfig(method="oauth")),
        )
    )

    assert resolver.methods("google").default_method == "service_account"
    assert resolver.methods("copilot").default_method == "oauth"


def test_provider_auth_authorize_openai_from_provider_config_materializes_bearer_header() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(openai=OpenAIProviderConfig(api_key="openai-secret"))
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="openai"))

    assert result.status == "authorized"
    assert result.material is not None
    assert result.material.headers == {"Authorization": "Bearer openai-secret"}


def test_provider_auth_authorize_google_oauth_needs_callback_when_access_token_missing() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="oauth"))
        )
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="google"))

    assert result.status == "needs_callback"
    assert result.callback is not None
    assert result.callback.state.startswith("voidcode:google:oauth:callback:")
    assert result.material is None


def test_provider_auth_authorize_google_service_account_returns_provider_ready_metadata() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            google=GoogleProviderConfig(
                auth=GoogleProviderAuthConfig(
                    method="service_account",
                    service_account_json_path="/tmp/service-account.json",
                )
            )
        )
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="google"))

    assert result.status == "authorized"
    assert result.material is not None
    assert result.material.metadata == {"service_account_json_path": "/tmp/service-account.json"}


def test_provider_auth_authorize_copilot_token_reads_configured_env_var() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            copilot=CopilotProviderConfig(
                auth=CopilotProviderAuthConfig(method="token", token_env_var="COPILOT_TOKEN")
            )
        ),
        env={"COPILOT_TOKEN": "copilot-secret"},
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="copilot"))

    assert result.status == "authorized"
    assert result.material is not None
    assert result.material.headers == {"Authorization": "Bearer copilot-secret"}


def test_provider_auth_authorize_rejects_config_method_mismatch_deterministically() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="service_account"))
        )
    )

    with pytest.raises(
        ProviderAuthResolutionError,
        match="must match configured method 'service_account'",
    ) as exc_info:
        _ = resolver.authorize(ProviderAuthAuthorizeRequest(provider="google", method="api_key"))

    assert exc_info.value.code == "invalid_payload"
    assert provider_auth_error_to_execution_kind(exc_info.value) == "invalid_model"


def test_provider_auth_authorize_missing_credentials_raises_deterministic_error() -> None:
    resolver = ProviderAuthResolver(providers=ProviderConfigs())

    with pytest.raises(
        ProviderAuthResolutionError,
        match="provider auth field 'openai.api_key' must be provided",
    ) as exc_info:
        _ = resolver.authorize(ProviderAuthAuthorizeRequest(provider="openai"))

    assert exc_info.value.code == "missing_credentials"
    assert provider_auth_error_to_execution_kind(exc_info.value) == "invalid_model"


def test_provider_auth_callback_google_oauth_builds_auth_material() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="oauth"))
        )
    )

    authorize_result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="google"))
    assert authorize_result.status == "needs_callback"
    assert authorize_result.callback is not None

    material = resolver.callback(
        ProviderAuthCallbackRequest(
            provider="google",
            method="oauth",
            state=authorize_result.callback.state,
            payload={"access_token": "google-oauth-token"},
        )
    )

    assert material.headers == {"Authorization": "Bearer google-oauth-token"}


def test_provider_auth_callback_invalid_state_rejects_deterministically() -> None:
    resolver = ProviderAuthResolver(providers=ProviderConfigs())

    with pytest.raises(ProviderAuthResolutionError, match="callback state.*is invalid") as exc_info:
        _ = resolver.callback(
            ProviderAuthCallbackRequest(
                provider="google",
                method="oauth",
                state="invalid-state",
                payload={"access_token": "x"},
            )
        )

    assert exc_info.value.code == "invalid_state"


def test_provider_auth_callback_not_supported_for_non_callback_method() -> None:
    resolver = ProviderAuthResolver(providers=ProviderConfigs())

    with pytest.raises(
        ProviderAuthResolutionError,
        match="callback is not supported for provider 'openai' method 'api_key'",
    ) as exc_info:
        _ = resolver.callback(
            ProviderAuthCallbackRequest(
                provider="openai",
                method="api_key",
                state="voidcode:openai:api_key:callback",
                payload={"access_token": "x"},
            )
        )

    assert exc_info.value.code == "callback_not_supported"
