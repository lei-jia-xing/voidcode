from __future__ import annotations

import pytest

from voidcode.provider.config import (
    AnthropicProviderConfig,
    CopilotProviderAuthConfig,
    CopilotProviderConfig,
    GoogleProviderAuthConfig,
    GoogleProviderConfig,
    OpenAICompatibleProviderConfig,
    OpenAIProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    ProviderFallbackConfig,
    ProviderTransientRetryConfig,
    merge_provider_configs,
    parse_provider_configs_payload,
    parse_provider_fallback_payload,
    provider_configs_from_env,
    serialize_provider_configs,
)


def test_parse_provider_configs_payload_parses_provider_blocks_directly() -> None:
    parsed = parse_provider_configs_payload(
        {
            "openai": {"base_url": "https://api.openai.test"},
            "anthropic": {"discovery_base_url": "https://api.anthropic.com"},
            "google": {
                "auth": {"method": "api_key"},
                "discovery_base_url": "https://generativelanguage.googleapis.com",
            },
            "copilot": {
                "auth": {
                    "method": "oauth",
                    "token_env_var": "COPILOT_TOKEN",
                    "refresh_token": "refresh-token",
                    "refresh_leeway_seconds": 30,
                }
            },
            "endpoint": {
                "base_url": "http://localhost:4000",
                "auth_scheme": "token",
                "api_key_env_var": "ENDPOINT_KEY",
                "model_map": {"gpt-4o": "openrouter/openai/gpt-4o"},
                "transient_retry": {
                    "max_retries": 3,
                    "base_delay_ms": 250,
                    "max_delay_ms": 2000,
                    "jitter": False,
                },
            },
            "opencode": {
                "auth_scheme": "none",
                "transient_retry": {"max_retries": 0},
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
            "ENDPOINT_KEY": "endpoint-env-key",
        },
    )

    assert parsed == ProviderConfigs(
        openai=OpenAIProviderConfig(
            api_key="openai-env-key",
            base_url="https://api.openai.test",
            discovery_base_url=None,
        ),
        anthropic=AnthropicProviderConfig(
            api_key="anthropic-env-key",
            discovery_base_url="https://api.anthropic.com",
        ),
        google=GoogleProviderConfig(
            auth=GoogleProviderAuthConfig(method="api_key", api_key="google-env-key"),
            discovery_base_url="https://generativelanguage.googleapis.com",
        ),
        copilot=CopilotProviderConfig(
            auth=CopilotProviderAuthConfig(
                method="oauth",
                token_env_var="COPILOT_TOKEN",
                refresh_token="refresh-token",
                refresh_leeway_seconds=30,
            )
        ),
        endpoint=ProviderEndpointConfig(
            api_key="endpoint-env-key",
            api_key_env_var="ENDPOINT_KEY",
            base_url="http://localhost:4000",
            auth_scheme="token",
            model_map={"gpt-4o": "openrouter/openai/gpt-4o"},
            transient_retry=ProviderTransientRetryConfig(
                max_retries=3,
                base_delay_ms=250.0,
                max_delay_ms=2000.0,
                jitter=False,
            ),
        ),
        opencode=ProviderEndpointConfig(
            auth_scheme="none",
            auth_scheme_explicit=True,
            transient_retry=ProviderTransientRetryConfig(max_retries=0),
        ),
        custom={
            "llama-local": ProviderEndpointConfig(
                base_url="http://localhost:11434/v1",
                auth_scheme="none",
                model_map={"coder": "ollama/qwen2.5-coder:latest"},
            )
        },
    )

    assert parsed is not None
    assert parsed.endpoint is not None
    assert parsed.endpoint.auth_scheme_explicit is True
    assert parsed.opencode is not None
    assert parsed.opencode.auth_scheme_explicit is True
    assert parsed.custom["llama-local"].auth_scheme_explicit is True


def test_parse_provider_configs_payload_parses_transient_retry_for_openai_compatible_provider() -> None:
    parsed = parse_provider_configs_payload(
        {
            "opencode-go": {
                "api_key_env_var": "OPENCODE_API_KEY",
                "transient_retry": {
                    "max_retries": 4,
                    "base_delay_ms": 500,
                    "max_delay_ms": 4000,
                    "jitter": False,
                },
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed == ProviderConfigs(
        opencode_go=OpenAICompatibleProviderConfig(
            api_key_env_var="OPENCODE_API_KEY",
            transient_retry=ProviderTransientRetryConfig(
                max_retries=4,
                base_delay_ms=500.0,
                max_delay_ms=4000.0,
                jitter=False,
            ),
        )
    )


def test_parse_provider_configs_payload_parses_ssl_verify_for_openai_compatible_provider() -> None:
    parsed = parse_provider_configs_payload(
        {
            "opencode-go": {
                "api_key": "opencode-key",
                "ssl_verify": False,
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed == ProviderConfigs(opencode_go=OpenAICompatibleProviderConfig(api_key="opencode-key", ssl_verify=False))


def test_parse_provider_configs_payload_rejects_unknown_provider_block() -> None:
    # An undeclared provider id in the config names the canonical ids and the one
    # supported way to add a custom OpenAI-compatible endpoint.
    with pytest.raises(
        ValueError,
        match=(
            r"runtime config field 'providers.unknown': unknown provider id 'unknown': "
            r"known provider ids are .*minimax.*; "
            r"declare a custom OpenAI-compatible endpoint as providers\.custom\.unknown"
        ),
    ):
        _ = parse_provider_configs_payload(
            {"unknown": {}},
            source="runtime config field 'providers'",
        )


def test_parse_provider_configs_payload_canonicalises_builtin_provider_keys() -> None:
    # `providers.MiniMax` and `providers.minimax` are one entry: the id is
    # case-insensitive at the config boundary.
    canonical = parse_provider_configs_payload(
        {"minimax": {"api_key": "sk-canonical"}},
        source="runtime config field 'providers'",
    )
    variant = parse_provider_configs_payload(
        {"MiniMax": {"api_key": "sk-canonical"}},
        source="runtime config field 'providers'",
    )

    assert variant == canonical
    assert variant is not None
    assert variant.minimax is not None
    assert variant.minimax.api_key == "sk-canonical"


def test_parse_provider_configs_payload_rejects_duplicate_case_variant_provider_blocks() -> None:
    with pytest.raises(
        ValueError,
        match=r"providers\.minimax' duplicates runtime config field 'providers\.MiniMax'",
    ):
        _ = parse_provider_configs_payload(
            {"MiniMax": {"api_key": "a"}, "minimax": {"api_key": "b"}},
            source="runtime config field 'providers'",
        )


def test_parse_provider_configs_payload_rejects_legacy_litellm_block_with_rename_hint() -> None:
    with pytest.raises(
        ValueError,
        match=r"runtime config field 'providers\.litellm' is not supported; use 'providers\.endpoint' instead",
    ):
        _ = parse_provider_configs_payload(
            {"litellm": {}},
            source="runtime config field 'providers'",
        )


@pytest.mark.parametrize("builtin_name", ["openai", "anthropic", "google", "copilot", "endpoint", "opencode"])
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


def test_custom_provider_name_is_canonicalised_to_lowercase() -> None:
    parsed = parse_provider_configs_payload(
        {
            "custom": {
                "Local-GW": {
                    "base_url": "http://localhost:11434/v1",
                }
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert "local-gw" in parsed.custom


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
    with pytest.raises(ValueError, match="provider fallback chain must not contain duplicate models"):
        _ = parse_provider_fallback_payload(
            {
                "preferred_model": "opencode/gpt-5.4",
                "fallback_models": ["opencode/gpt-5.4"],
            },
            source="runtime config field 'provider_fallback'",
        )


# =============================================================================
# OpenAI-compatible Provider Config Tests
# =============================================================================


@pytest.mark.parametrize(
    ("provider_id", "attribute", "env", "expected_api_key"),
    (
        pytest.param("deepseek", "deepseek", {"DEEPSEEK_API_KEY": "deepseek-env-key"}, "deepseek-env-key", id="deepseek"),
        pytest.param("zai", "zai", {"ZAI_API_KEY": "zai-env-key"}, "zai-env-key", id="zai"),
        pytest.param(
            "zhipuai",
            "zhipuai",
            {"ZAI_API_KEY": "zai-env-key", "ZHIPU_API_KEY": "zhipu-env-key"},
            "zhipu-env-key",
            id="zhipuai-prefers-own-key",
        ),
        pytest.param("zhipuai", "zhipuai", {"ZAI_API_KEY": "zai-env-key"}, "zai-env-key", id="zhipuai-falls-back-to-zai"),
        pytest.param("grok", "grok", {"XAI_API_KEY": "xai-env-key"}, "xai-env-key", id="grok"),
        # The removed GROK_API_KEY spelling must never populate the provider.
        pytest.param("grok", "grok", {"GROK_API_KEY": "grok-env-key"}, None, id="grok-removed-env-var-ignored"),
        pytest.param("minimax", "minimax", {"MINIMAX_API_KEY": "minimax-env-key"}, "minimax-env-key", id="minimax"),
        pytest.param("kimi", "kimi", {"KIMI_API_KEY": "kimi-env-key"}, "kimi-env-key", id="kimi"),
        pytest.param(
            "opencode-go",
            "opencode_go",
            {"OPENCODE_API_KEY": "opencode-go-env-key"},
            "opencode-go-env-key",
            id="opencode-go",
        ),
        pytest.param("qwen", "qwen", {"DASHSCOPE_API_KEY": "qwen-env-key"}, "qwen-env-key", id="qwen"),
    ),
)
def test_parse_provider_config_from_env(
    provider_id: str,
    attribute: str,
    env: dict[str, str],
    expected_api_key: str | None,
) -> None:
    parsed = parse_provider_configs_payload(
        {provider_id: {}},
        source="runtime config field 'providers'",
        env=env,
    )

    assert parsed is not None
    assert getattr(parsed, attribute) == OpenAICompatibleProviderConfig(api_key=expected_api_key)


@pytest.mark.parametrize(
    ("env", "attribute", "expected_api_key"),
    (
        pytest.param({"OPENCODE_API_KEY": "opencode-go-env-key"}, "opencode_go", "opencode-go-env-key", id="opencode-go"),
        pytest.param({"DEEPSEEK_API_KEY": "deepseek-env-key"}, "deepseek", "deepseek-env-key", id="deepseek"),
        pytest.param({"XAI_API_KEY": "xai-env-key"}, "grok", "xai-env-key", id="grok"),
        pytest.param({"ZAI_API_KEY": "zai-env-key"}, "zai", "zai-env-key", id="zai"),
        pytest.param({"ZHIPU_API_KEY": "zhipu-env-key"}, "zhipuai", "zhipu-env-key", id="zhipuai"),
        pytest.param({"DASHSCOPE_API_KEY": "qwen-env-key"}, "qwen", "qwen-env-key", id="qwen"),
    ),
)
def test_provider_configs_from_env_builds_configured_provider(
    env: dict[str, str],
    attribute: str,
    expected_api_key: str,
) -> None:
    parsed = provider_configs_from_env(env)

    assert parsed is not None
    assert getattr(parsed, attribute) == OpenAICompatibleProviderConfig(api_key=expected_api_key)


def test_provider_configs_from_env_leaves_unconfigured_providers_unset() -> None:
    other_provider_env = provider_configs_from_env({"ZAI_API_KEY": "zai-env-key"})

    assert other_provider_env is not None
    assert other_provider_env.qwen is None


def test_merge_provider_configs_keeps_repo_provider_over_environment_fallback() -> None:
    merged = merge_provider_configs(
        ProviderConfigs(opencode_go=OpenAICompatibleProviderConfig(api_key="repo-key")),
        ProviderConfigs(opencode_go=OpenAICompatibleProviderConfig(api_key="env-key")),
    )

    assert merged is not None
    assert merged.opencode_go == OpenAICompatibleProviderConfig(api_key="repo-key")


def test_new_provider_configs_serialize_without_secrets() -> None:
    payload = serialize_provider_configs(
        ProviderConfigs(
            groq=OpenAICompatibleProviderConfig(api_key="groq-secret"),
            together=OpenAICompatibleProviderConfig(api_key="together-secret"),
            fireworks=OpenAICompatibleProviderConfig(api_key="fireworks-secret"),
            mistral=OpenAICompatibleProviderConfig(api_key="mistral-secret"),
        )
    )
    assert payload == {
        "groq": {},
        "together": {},
        "fireworks": {},
        "mistral": {},
    }
