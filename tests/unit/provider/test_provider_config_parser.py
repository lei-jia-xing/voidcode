from __future__ import annotations

import pytest

from voidcode.provider.config import (
    AnthropicProviderConfig,
    CopilotProviderAuthConfig,
    CopilotProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderFallbackConfig,
    parse_provider_configs_payload,
    parse_provider_fallback_payload,
)


def test_parse_provider_configs_payload_parses_provider_blocks_directly() -> None:
    parsed = parse_provider_configs_payload(
        {
            "openai": {"base_url": "https://api.openai.test"},
            "anthropic": {},
            "google": {"auth": {"method": "api_key"}},
            "copilot": {
                "auth": {
                    "method": "oauth",
                    "token_env_var": "COPILOT_TOKEN",
                    "refresh_token": "refresh-token",
                    "refresh_leeway_seconds": 30,
                }
            },
            "litellm": {
                "base_url": "http://localhost:4000",
                "auth_scheme": "token",
                "api_key_env_var": "LITELLM_KEY",
                "model_map": {"gpt-4o": "openrouter/openai/gpt-4o"},
            },
            "custom": {
                "llama-local": {
                    "base_url": "http://localhost:11434/v1",
                    "auth_scheme": "none",
                    "model_map": {"coder": "ollama/qwen2.5-coder:latest"},
                }
            },
        },
        source="runtime config field 'providers'",
        env={
            "OPENAI_API_KEY": "openai-env-key",
            "ANTHROPIC_API_KEY": "anthropic-env-key",
            "GOOGLE_API_KEY": "google-env-key",
            "LITELLM_KEY": "litellm-env-key",
        },
    )

    assert parsed == ProviderConfigs(
        openai=OpenAIProviderConfig(
            api_key="openai-env-key",
            base_url="https://api.openai.test",
        ),
        anthropic=AnthropicProviderConfig(api_key="anthropic-env-key"),
        google=GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(method="api_key", api_key="google-env-key")
        ),
        copilot=CopilotProviderConfig(
            auth=CopilotProviderAuthConfig(
                method="oauth",
                token_env_var="COPILOT_TOKEN",
                refresh_token="refresh-token",
                refresh_leeway_seconds=30,
            )
        ),
        litellm=LiteLLMProviderConfig(
            api_key="litellm-env-key",
            api_key_env_var="LITELLM_KEY",
            base_url="http://localhost:4000",
            auth_scheme="token",
            model_map={"gpt-4o": "openrouter/openai/gpt-4o"},
        ),
        custom={
            "llama-local": LiteLLMProviderConfig(
                base_url="http://localhost:11434/v1",
                auth_scheme="none",
                model_map={"coder": "ollama/qwen2.5-coder:latest"},
            )
        },
    )


def test_parse_provider_configs_payload_rejects_unknown_provider_block() -> None:
    with pytest.raises(
        ValueError, match="runtime config field 'providers.unknown' is not supported"
    ):
        _ = parse_provider_configs_payload(
            {"unknown": {}},
            source="runtime config field 'providers'",
        )


def test_parse_provider_configs_payload_rejects_invalid_custom_provider_name() -> None:
    with pytest.raises(
        ValueError,
        match="runtime config field 'providers.custom.invalid/name'",
    ):
        _ = parse_provider_configs_payload(
            {
                "custom": {
                    "invalid/name": {
                        "base_url": "http://localhost:4000",
                    }
                }
            },
            source="runtime config field 'providers'",
        )


@pytest.mark.parametrize(
    "builtin_name", ["openai", "anthropic", "google", "copilot", "litellm", "opencode"]
)
def test_parse_provider_configs_payload_rejects_custom_provider_name_colliding_with_builtin(
    builtin_name: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=(
            rf"runtime config field 'providers.custom\.{builtin_name}' "
            rf"must not collide with built-in provider names \(conflicts with '{builtin_name}'\)"
        ),
    ):
        _ = parse_provider_configs_payload(
            {
                "custom": {
                    builtin_name: {
                        "base_url": "http://localhost:4000",
                    }
                }
            },
            source="runtime config field 'providers'",
        )


def test_parse_provider_configs_payload_rejects_case_or_whitespace_variant_of_builtin_name() -> (
    None
):
    with pytest.raises(
        ValueError,
        match=(
            r"runtime config field 'providers.custom\. OpenAI ' "
            r"must not have leading or trailing whitespace"
        ),
    ):
        _ = parse_provider_configs_payload(
            {
                "custom": {
                    " OpenAI ": {
                        "base_url": "http://localhost:4000",
                    }
                }
            },
            source="runtime config field 'providers'",
        )


def test_custom_provider_name_with_surrounding_whitespace_rejected() -> None:
    with pytest.raises(
        ValueError,
        match=(
            r"runtime config field 'providers.custom\. llama-local ' "
            r"must not have leading or trailing whitespace"
        ),
    ):
        _ = parse_provider_configs_payload(
            {
                "custom": {
                    " llama-local ": {
                        "base_url": "http://localhost:11434/v1",
                    }
                }
            },
            source="runtime config field 'providers'",
        )


def test_parse_provider_fallback_payload_parses_chain_directly() -> None:
    parsed = parse_provider_fallback_payload(
        {
            "preferred_model": "opencode/gpt-5.4",
            "fallback_models": ["openai/gpt-4.1", "anthropic/claude-3-7-sonnet"],
        },
        source="runtime config field 'provider_fallback'",
    )

    assert parsed == ProviderFallbackConfig(
        preferred_model="opencode/gpt-5.4",
        fallback_models=("openai/gpt-4.1", "anthropic/claude-3-7-sonnet"),
    )


def test_parse_provider_fallback_payload_rejects_duplicate_chain_models() -> None:
    with pytest.raises(
        ValueError, match="provider fallback chain must not contain duplicate models"
    ):
        _ = parse_provider_fallback_payload(
            {
                "preferred_model": "opencode/gpt-5.4",
                "fallback_models": ["opencode/gpt-5.4"],
            },
            source="runtime config field 'provider_fallback'",
        )
