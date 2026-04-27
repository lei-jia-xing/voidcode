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
    LiteLLMProviderConfig,
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
    assert resolver.methods("litellm").default_method == "none"
    assert [method.id for method in resolver.methods("litellm").methods] == ["api_key", "none"]


def test_provider_auth_methods_discovery_supports_custom_provider_from_config() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            custom={
                "llama-local": LiteLLMProviderConfig(
                    api_key="custom-secret",
                    auth_scheme="bearer",
                )
            }
        )
    )

    methods = resolver.methods("llama-local")

    assert methods.provider == "llama-local"
    assert methods.default_method == "api_key"
    assert [method.id for method in methods.methods] == ["api_key", "none"]


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
    assert provider_auth_error_to_execution_kind(exc_info.value) == "missing_auth"


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


def test_provider_auth_authorize_litellm_with_api_key_returns_bearer_material() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(litellm=LiteLLMProviderConfig(api_key="litellm-secret"))
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="litellm", method="api_key"))

    assert result.status == "authorized"
    assert result.material is not None
    assert result.material.headers == {"Authorization": "Bearer litellm-secret"}


def test_provider_auth_methods_rejects_unconfigured_custom_provider_name() -> None:
    resolver = ProviderAuthResolver(providers=ProviderConfigs())

    with pytest.raises(
        ProviderAuthResolutionError,
        match="provider auth provider 'llama-local' is not supported",
    ) as exc_info:
        _ = resolver.methods("llama-local")

    assert exc_info.value.code == "unsupported_provider"


def test_provider_auth_authorize_rejects_unconfigured_custom_provider_name() -> None:
    resolver = ProviderAuthResolver(providers=ProviderConfigs())

    with pytest.raises(
        ProviderAuthResolutionError,
        match="provider auth provider 'llama-local' is not supported",
    ) as exc_info:
        _ = resolver.authorize(ProviderAuthAuthorizeRequest(provider="llama-local"))

    assert exc_info.value.code == "unsupported_provider"


def test_provider_auth_callback_rejects_custom_provider_before_state_validation() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(custom={"llama-local": LiteLLMProviderConfig(api_key="secret")})
    )

    with pytest.raises(
        ProviderAuthResolutionError,
        match="callback is not supported for provider 'llama-local' method 'oauth'",
    ) as exc_info:
        _ = resolver.callback(
            ProviderAuthCallbackRequest(
                provider="llama-local",
                method="oauth",
                state="voidcode:llama-local:oauth:callback:fake",
                payload={"access_token": "x"},
            )
        )

    assert exc_info.value.code == "callback_not_supported"


def test_provider_auth_authorize_litellm_without_api_key_allows_none_mode() -> None:
    resolver = ProviderAuthResolver(providers=ProviderConfigs(litellm=LiteLLMProviderConfig()))

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="litellm"))

    assert result.status == "authorized"
    assert result.method == "none"
    assert result.material is not None
    assert result.material.headers == {}


def test_provider_auth_authorize_custom_provider_with_api_key_returns_bearer_material() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            custom={"llama-local": LiteLLMProviderConfig(api_key="custom-secret")}
        )
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="llama-local"))

    assert result.status == "authorized"
    assert result.provider == "llama-local"
    assert result.material is not None
    assert result.material.headers == {"Authorization": "Bearer custom-secret"}


def test_provider_auth_authorize_custom_provider_none_mode_returns_empty_headers() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(custom={"llama-local": LiteLLMProviderConfig(auth_scheme="none")})
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="llama-local"))

    assert result.status == "authorized"
    assert result.method == "none"
    assert result.material is not None
    assert result.material.headers == {}


def test_provider_auth_authorize_custom_provider_respects_explicit_method_override() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            custom={"llama-local": LiteLLMProviderConfig(api_key="custom-secret")}
        )
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="llama-local", method="none"))

    assert result.status == "authorized"
    assert result.method == "none"
    assert result.material is not None
    assert result.material.headers == {}


def test_provider_auth_custom_provider_matches_litellm_behavior_for_methods_and_authorize() -> None:
    providers = ProviderConfigs(
        litellm=LiteLLMProviderConfig(api_key="same-secret"),
        custom={"llama-local": LiteLLMProviderConfig(api_key="same-secret")},
    )
    resolver = ProviderAuthResolver(providers=providers)

    litellm_methods = resolver.methods("litellm")
    custom_methods = resolver.methods("llama-local")
    assert [method.id for method in custom_methods.methods] == [
        method.id for method in litellm_methods.methods
    ]
    assert custom_methods.default_method == litellm_methods.default_method

    litellm_auth = resolver.authorize(ProviderAuthAuthorizeRequest(provider="litellm"))
    custom_auth = resolver.authorize(ProviderAuthAuthorizeRequest(provider="llama-local"))
    assert litellm_auth.status == custom_auth.status == "authorized"
    assert litellm_auth.material is not None
    assert custom_auth.material is not None
    assert litellm_auth.material.headers == custom_auth.material.headers


def test_provider_auth_methods_litellm_prefers_none_when_auth_scheme_is_none() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            litellm=LiteLLMProviderConfig(api_key="litellm-secret", auth_scheme="none")
        )
    )

    methods = resolver.methods("litellm")

    assert methods.default_method == "none"


def test_provider_auth_authorize_litellm_defaults_to_none_when_auth_scheme_is_none() -> None:
    resolver = ProviderAuthResolver(
        providers=ProviderConfigs(
            litellm=LiteLLMProviderConfig(api_key="litellm-secret", auth_scheme="none")
        )
    )

    result = resolver.authorize(ProviderAuthAuthorizeRequest(provider="litellm"))

    assert result.method == "none"
    assert result.material is not None
    assert result.material.headers == {}
