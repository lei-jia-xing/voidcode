from __future__ import annotations

from voidcode.provider.auth import ProviderAuthResolver
from voidcode.provider.config import (
    CopilotProviderAuthConfig,
    CopilotProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
)
from voidcode.runtime.provider_inspection import RuntimeProviderAuthInspector


def _inspector(
    providers: ProviderConfigs | None,
    *,
    env: dict[str, str] | None = None,
) -> RuntimeProviderAuthInspector:
    environment = {} if env is None else env
    return RuntimeProviderAuthInspector(
        providers=providers,
        resolver=ProviderAuthResolver(providers=providers, env=environment),
        env=environment,
    )


def test_provider_auth_inspector_reports_configured_builtin_and_custom_providers() -> None:
    inspector = _inspector(
        ProviderConfigs(
            openai=OpenAIProviderConfig(),
            custom={"local": LiteLLMProviderConfig()},
        )
    )

    assert inspector.is_configured("openai") is True
    assert inspector.is_configured("anthropic") is False
    assert inspector.is_configured("local") is True


def test_provider_auth_inspector_maps_missing_credentials_to_missing_auth() -> None:
    presence = _inspector(ProviderConfigs(openai=OpenAIProviderConfig())).presence("openai")

    assert presence.present is False
    assert presence.failure_kind == "missing_auth"
    assert presence.message is not None
    assert "openai.api_key" in presence.message


def test_provider_auth_inspector_checks_google_oauth_without_callback_allocation() -> None:
    providers = ProviderConfigs(google=GoogleProviderConfig(auth=GoogleProviderAuthConfig(method="oauth", access_token="token")))
    resolver = ProviderAuthResolver(providers=providers, env={})
    inspector = RuntimeProviderAuthInspector(providers=providers, resolver=resolver, env={})

    presence = inspector.presence("google")

    assert presence.present is True
    assert resolver._pending_callback_states == {}


def test_provider_auth_inspector_reads_copilot_oauth_token_environment() -> None:
    providers = ProviderConfigs(copilot=CopilotProviderConfig(auth=CopilotProviderAuthConfig(method="oauth", token_env_var="COPILOT_TEST_TOKEN")))

    presence = _inspector(providers, env={"COPILOT_TEST_TOKEN": "token"}).presence("copilot")

    assert presence.present is True


def test_provider_auth_inspector_preserves_invalid_provider_failure() -> None:
    presence = _inspector(ProviderConfigs()).presence("unknown")

    assert presence.present is False
    assert presence.failure_kind == "invalid_model"
    assert presence.message is not None
