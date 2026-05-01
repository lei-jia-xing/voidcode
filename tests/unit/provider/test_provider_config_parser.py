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
    ProviderTransientRetryConfig,
    SimplifiedProviderConfig,
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
            "litellm": {
                "base_url": "http://localhost:4000",
                "auth_scheme": "token",
                "api_key_env_var": "LITELLM_KEY",
                "model_map": {"gpt-4o": "openrouter/openai/gpt-4o"},
                "transient_retry": {
                    "max_retries": 3,
                    "base_delay_ms": 250,
                    "max_delay_ms": 2000,
                    "jitter": False,
                },
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
        litellm=LiteLLMProviderConfig(
            api_key="litellm-env-key",
            api_key_env_var="LITELLM_KEY",
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
        custom={
            "llama-local": LiteLLMProviderConfig(
                base_url="http://localhost:11434/v1",
                auth_scheme="none",
                model_map={"coder": "ollama/qwen2.5-coder:latest"},
            )
        },
    )


def test_parse_provider_configs_payload_parses_transient_retry_for_simplified_provider() -> None:
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
        opencode_go=SimplifiedProviderConfig(
            api_key_env_var="OPENCODE_API_KEY",
            transient_retry=ProviderTransientRetryConfig(
                max_retries=4,
                base_delay_ms=500.0,
                max_delay_ms=4000.0,
                jitter=False,
            ),
        )
    )


def test_parse_provider_configs_payload_rejects_invalid_transient_retry_delay_order() -> None:
    with pytest.raises(
        ValueError,
        match=(
            r"runtime config field 'providers\.opencode-go\.transient_retry\.max_delay_ms' "
            r"must be greater than or equal to base_delay_ms"
        ),
    ):
        _ = parse_provider_configs_payload(
            {
                "opencode-go": {
                    "transient_retry": {
                        "base_delay_ms": 2000,
                        "max_delay_ms": 1000,
                    }
                }
            },
            source="runtime config field 'providers'",
        )


def test_parse_provider_configs_payload_rejects_unknown_provider_block() -> None:
    with pytest.raises(
        ValueError, match="runtime config field 'providers.unknown' is not supported"
    ):
        _ = parse_provider_configs_payload(
            {"unknown": {}},
            source="runtime config field 'providers'",
        )


def test_merge_provider_configs_preserves_fallback_litellm_none_auth_scheme() -> None:
    primary = ProviderConfigs(litellm=LiteLLMProviderConfig(api_key="env-key"))
    fallback = ProviderConfigs(litellm=LiteLLMProviderConfig(auth_scheme="none"))

    merged = merge_provider_configs(primary, fallback)

    assert merged is not None
    assert merged.litellm == LiteLLMProviderConfig(
        api_key="env-key",
        auth_scheme="none",
    )


def test_merge_provider_configs_preserves_fallback_litellm_token_auth_scheme() -> None:
    primary = ProviderConfigs(litellm=LiteLLMProviderConfig(api_key="env-key"))
    fallback = ProviderConfigs(
        litellm=LiteLLMProviderConfig(auth_scheme="token", auth_header="X-API-Key")
    )

    merged = merge_provider_configs(primary, fallback)

    assert merged is not None
    assert merged.litellm == LiteLLMProviderConfig(
        api_key="env-key",
        auth_header="X-API-Key",
        auth_scheme="token",
    )


def test_merge_provider_configs_allows_empty_anthropic_beta_headers_override() -> None:
    primary = parse_provider_configs_payload(
        {"anthropic": {"beta_headers": []}},
        source="runtime config field 'providers'",
    )
    fallback = ProviderConfigs(anthropic=AnthropicProviderConfig(beta_headers=("stale-beta",)))

    merged = merge_provider_configs(primary, fallback)

    assert merged is not None
    assert merged.anthropic == AnthropicProviderConfig(
        beta_headers=(),
        beta_headers_explicit=True,
    )


def test_merge_provider_configs_preserves_anthropic_beta_headers_when_absent() -> None:
    primary = parse_provider_configs_payload(
        {"anthropic": {"api_key": "repo-key"}},
        source="runtime config field 'providers'",
    )
    fallback = ProviderConfigs(anthropic=AnthropicProviderConfig(beta_headers=("fallback-beta",)))

    merged = merge_provider_configs(primary, fallback)

    assert merged is not None
    assert merged.anthropic == AnthropicProviderConfig(
        api_key="repo-key",
        beta_headers=("fallback-beta",),
    )


def test_serialize_provider_configs_preserves_explicit_empty_anthropic_beta_headers() -> None:
    payload = serialize_provider_configs(
        ProviderConfigs(
            anthropic=AnthropicProviderConfig(
                beta_headers=(),
                beta_headers_explicit=True,
            )
        )
    )

    assert payload == {"anthropic": {"beta_headers": []}}


def test_parse_provider_configs_payload_rejects_invalid_openai_base_url_type() -> None:
    with pytest.raises(
        ValueError,
        match=r"runtime config field 'providers\.openai\.base_url' must be a string when provided",
    ):
        _ = parse_provider_configs_payload(
            {"openai": {"base_url": 123}},
            source="runtime config field 'providers'",
        )


def test_parse_provider_configs_payload_rejects_invalid_litellm_model_map_value() -> None:
    with pytest.raises(
        ValueError,
        match=r"runtime config field 'providers\.litellm\.model_map\.demo' must be a string",
    ):
        _ = parse_provider_configs_payload(
            {"litellm": {"model_map": {"demo": 123}}},
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


# =============================================================================
# Simplified Provider Config Tests
# =============================================================================


def test_parse_deepseek_provider_config_from_env() -> None:
    parsed = parse_provider_configs_payload(
        {"deepseek": {}},
        source="runtime config field 'providers'",
        env={"DEEPSEEK_API_KEY": "deepseek-env-key"},
    )

    assert parsed is not None
    assert parsed.deepseek == SimplifiedProviderConfig(api_key="deepseek-env-key")


def test_parse_glm_provider_config_from_env() -> None:
    parsed = parse_provider_configs_payload(
        {"glm": {}},
        source="runtime config field 'providers'",
        env={"GLM_API_KEY": "glm-env-key"},
    )

    assert parsed is not None
    assert parsed.glm == SimplifiedProviderConfig(api_key="glm-env-key")


def test_parse_glm_provider_config_with_api_key() -> None:
    parsed = parse_provider_configs_payload(
        {"glm": {"api_key": "glm-direct-key"}},
        source="runtime config field 'providers'",
        env={"GLM_API_KEY": "glm-env-key"},
    )

    assert parsed is not None
    assert parsed.glm == SimplifiedProviderConfig(api_key="glm-direct-key")


def test_parse_glm_provider_config_with_base_url_and_model_map() -> None:
    parsed = parse_provider_configs_payload(
        {
            "glm": {
                "api_key": "glm-key",
                "base_url": "https://custom.glm.cn",
                "model_map": {"glm4": "glm-4-flash", "glm4-plus": "glm-4-plus"},
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert parsed.glm == SimplifiedProviderConfig(
        api_key="glm-key",
        base_url="https://custom.glm.cn",
        model_map={"glm4": "glm-4-flash", "glm4-plus": "glm-4-plus"},
    )


def test_parse_grok_provider_config_from_env() -> None:
    parsed = parse_provider_configs_payload(
        {"grok": {}},
        source="runtime config field 'providers'",
        env={"XAI_API_KEY": "xai-env-key"},
    )

    assert parsed is not None
    assert parsed.grok == SimplifiedProviderConfig(api_key="xai-env-key")


def test_parse_grok_provider_config_falls_back_to_grok_api_key() -> None:
    parsed = parse_provider_configs_payload(
        {"grok": {}},
        source="runtime config field 'providers'",
        env={"GROK_API_KEY": "grok-env-key"},
    )

    assert parsed is not None
    assert parsed.grok == SimplifiedProviderConfig(api_key="grok-env-key")


def test_parse_minimax_provider_config_from_env() -> None:
    parsed = parse_provider_configs_payload(
        {"minimax": {}},
        source="runtime config field 'providers'",
        env={"MINIMAX_API_KEY": "minimax-env-key"},
    )

    assert parsed is not None
    assert parsed.minimax == SimplifiedProviderConfig(api_key="minimax-env-key")


def test_parse_minimax_provider_config_with_timeout() -> None:
    parsed = parse_provider_configs_payload(
        {
            "minimax": {
                "api_key": "minimax-key",
                "timeout_seconds": 60.0,
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert parsed.minimax == SimplifiedProviderConfig(
        api_key="minimax-key",
        timeout_seconds=60.0,
    )


def test_parse_kimi_provider_config_from_env() -> None:
    parsed = parse_provider_configs_payload(
        {"kimi": {}},
        source="runtime config field 'providers'",
        env={"KIMI_API_KEY": "kimi-env-key"},
    )

    assert parsed is not None
    assert parsed.kimi == SimplifiedProviderConfig(api_key="kimi-env-key")


def test_parse_kimi_provider_config_with_base_url() -> None:
    parsed = parse_provider_configs_payload(
        {
            "kimi": {
                "api_key": "kimi-key",
                "base_url": "https://api.moonshot.cn/v1",
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert parsed.kimi == SimplifiedProviderConfig(
        api_key="kimi-key",
        base_url="https://api.moonshot.cn/v1",
    )


def test_parse_simplified_provider_config_with_discovery_base_url() -> None:
    parsed = parse_provider_configs_payload(
        {
            "kimi": {
                "api_key": "kimi-key",
                "base_url": "https://api.moonshot.ai",
                "discovery_base_url": "https://api.moonshot.ai/v1",
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert parsed.kimi == SimplifiedProviderConfig(
        api_key="kimi-key",
        base_url="https://api.moonshot.ai",
        discovery_base_url="https://api.moonshot.ai/v1",
    )


def test_parse_opencode_go_provider_config_from_env() -> None:
    parsed = parse_provider_configs_payload(
        {"opencode-go": {}},
        source="runtime config field 'providers'",
        env={"OPENCODE_API_KEY": "opencode-go-env-key"},
    )

    assert parsed is not None
    assert parsed.opencode_go == SimplifiedProviderConfig(api_key="opencode-go-env-key")


def test_parse_opencode_go_provider_config_with_model_map() -> None:
    parsed = parse_provider_configs_payload(
        {
            "opencode-go": {
                "api_key": "opencode-go-key",
                "model_map": {"gpt-4o": "opencode/gpt-4o", "claude": "opencode/claude-3-5-sonnet"},
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert parsed.opencode_go == SimplifiedProviderConfig(
        api_key="opencode-go-key",
        model_map={"gpt-4o": "opencode/gpt-4o", "claude": "opencode/claude-3-5-sonnet"},
    )


def test_parse_qwen_provider_config_from_env() -> None:
    parsed = parse_provider_configs_payload(
        {"qwen": {}},
        source="runtime config field 'providers'",
        env={"DASHSCOPE_API_KEY": "qwen-env-key"},
    )

    assert parsed is not None
    assert parsed.qwen == SimplifiedProviderConfig(api_key="qwen-env-key")


def test_parse_qwen_provider_config_with_base_url() -> None:
    parsed = parse_provider_configs_payload(
        {
            "qwen": {
                "api_key": "qwen-key",
                "base_url": "https://dashscope.aliyuncs.com",
            }
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert parsed.qwen == SimplifiedProviderConfig(
        api_key="qwen-key",
        base_url="https://dashscope.aliyuncs.com",
    )


def test_parse_multiple_simplified_providers_together() -> None:
    parsed = parse_provider_configs_payload(
        {
            "deepseek": {"api_key": "deepseek-key"},
            "glm": {"api_key": "glm-key"},
            "grok": {"api_key": "grok-key"},
            "minimax": {"api_key": "minimax-key"},
            "kimi": {"api_key": "kimi-key"},
            "opencode-go": {"api_key": "opencode-go-key"},
            "qwen": {"api_key": "qwen-key"},
        },
        source="runtime config field 'providers'",
    )

    assert parsed is not None
    assert parsed.deepseek == SimplifiedProviderConfig(api_key="deepseek-key")
    assert parsed.glm == SimplifiedProviderConfig(api_key="glm-key")
    assert parsed.grok == SimplifiedProviderConfig(api_key="grok-key")
    assert parsed.minimax == SimplifiedProviderConfig(api_key="minimax-key")
    assert parsed.kimi == SimplifiedProviderConfig(api_key="kimi-key")
    assert parsed.opencode_go == SimplifiedProviderConfig(api_key="opencode-go-key")
    assert parsed.qwen == SimplifiedProviderConfig(api_key="qwen-key")


def test_parse_simplified_provider_with_api_key_env_var_override() -> None:
    parsed = parse_provider_configs_payload(
        {
            "glm": {
                "api_key": "direct-key",
                "api_key_env_var": "MY_CUSTOM_GLM_KEY",
            }
        },
        source="runtime config field 'providers'",
        env={"MY_CUSTOM_GLM_KEY": "env-key", "GLM_API_KEY": "default-env-key"},
    )

    assert parsed is not None
    assert parsed.glm == SimplifiedProviderConfig(
        api_key="direct-key",
        api_key_env_var="MY_CUSTOM_GLM_KEY",
    )


def test_reject_unknown_simplified_provider() -> None:
    with pytest.raises(
        ValueError, match="runtime config field 'providers.unknown_cn' is not supported"
    ):
        _ = parse_provider_configs_payload(
            {"unknown_cn": {"api_key": "key"}},
            source="runtime config field 'providers'",
        )


@pytest.mark.parametrize(
    "provider_name",
    ["deepseek", "glm", "grok", "minimax", "kimi", "opencode-go", "qwen"],
)
def test_simplified_provider_not_allowed_in_custom_block(provider_name: str) -> None:
    with pytest.raises(
        ValueError,
        match=rf"runtime config field 'providers.custom\.{provider_name}'",
    ):
        _ = parse_provider_configs_payload(
            {
                "custom": {
                    provider_name: {"api_key": "key"},
                }
            },
            source="runtime config field 'providers'",
        )


def test_provider_configs_from_env_builds_opencode_go_without_repo_provider_block() -> None:
    parsed = provider_configs_from_env({"OPENCODE_API_KEY": "opencode-go-env-key"})

    assert parsed is not None
    assert parsed.opencode_go == SimplifiedProviderConfig(api_key="opencode-go-env-key")


def test_provider_configs_from_env_builds_deepseek_without_repo_provider_block() -> None:
    parsed = provider_configs_from_env({"DEEPSEEK_API_KEY": "deepseek-env-key"})

    assert parsed is not None
    assert parsed.deepseek == SimplifiedProviderConfig(api_key="deepseek-env-key")


def test_provider_configs_from_env_builds_grok_with_xai_api_key() -> None:
    parsed = provider_configs_from_env({"XAI_API_KEY": "xai-env-key"})

    assert parsed is not None
    assert parsed.grok == SimplifiedProviderConfig(api_key="xai-env-key")


def test_provider_configs_from_env_builds_glm_with_zai_api_key() -> None:
    parsed = provider_configs_from_env({"ZAI_API_KEY": "glm-env-key"})

    assert parsed is not None
    assert parsed.glm == SimplifiedProviderConfig(api_key="glm-env-key")


def test_provider_configs_from_env_builds_glm_with_zhipu_api_key() -> None:
    parsed = provider_configs_from_env({"ZHIPU_API_KEY": "glm-env-key"})

    assert parsed is not None
    assert parsed.glm == SimplifiedProviderConfig(api_key="glm-env-key")


def test_merge_provider_configs_keeps_repo_provider_over_environment_fallback() -> None:
    merged = merge_provider_configs(
        ProviderConfigs(opencode_go=SimplifiedProviderConfig(api_key="repo-key")),
        ProviderConfigs(opencode_go=SimplifiedProviderConfig(api_key="env-key")),
    )

    assert merged is not None
    assert merged.opencode_go == SimplifiedProviderConfig(api_key="repo-key")


def test_merge_provider_configs_preserves_empty_base_url_override() -> None:
    merged = merge_provider_configs(
        ProviderConfigs(
            openai=OpenAIProviderConfig(base_url="", discovery_base_url=""),
            litellm=LiteLLMProviderConfig(base_url="", discovery_base_url=""),
            opencode_go=SimplifiedProviderConfig(base_url="", discovery_base_url=""),
        ),
        ProviderConfigs(
            openai=OpenAIProviderConfig(
                base_url="https://fallback.openai.example",
                discovery_base_url="https://fallback.openai.example/v1",
            ),
            litellm=LiteLLMProviderConfig(
                base_url="https://fallback.litellm.example",
                discovery_base_url="https://fallback.litellm.example/v1",
            ),
            opencode_go=SimplifiedProviderConfig(
                base_url="https://fallback.opencode.example",
                discovery_base_url="https://fallback.opencode.example/v1",
            ),
        ),
    )

    assert merged is not None
    assert merged.openai is not None
    assert merged.openai.base_url == ""
    assert merged.openai.discovery_base_url == ""
    assert merged.litellm is not None
    assert merged.litellm.base_url == ""
    assert merged.litellm.discovery_base_url == ""
    assert merged.opencode_go is not None
    assert merged.opencode_go.base_url == ""
    assert merged.opencode_go.discovery_base_url == ""
