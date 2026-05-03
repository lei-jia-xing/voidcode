from __future__ import annotations

from .config import (
    AnthropicProviderConfig,
    CopilotProviderConfig,
    GoogleProviderConfig,
    LiteLLMProviderConfig,
    OpenAIProviderConfig,
)

_DEFAULT_DISCOVERY_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "litellm": "http://127.0.0.1:4000",
}

_DEFAULT_MODEL_MAPS: dict[str, dict[str, str]] = {
    "openai": {
        "gpt-5.5": "gpt-5.5",
        "gpt-5.4": "gpt-5.4",
        "gpt-5.4-mini": "gpt-5.4-mini",
        "gpt-5.4-nano": "gpt-5.4-nano",
        "gpt-5.2": "gpt-5.2",
        "gpt-5.2-pro": "gpt-5.2-pro",
        "gpt-5.2-codex": "gpt-5.2-codex",
        "gpt-5.1": "gpt-5.1",
        "gpt-5.1-codex": "gpt-5.1-codex",
        "gpt-5.1-codex-max": "gpt-5.1-codex-max",
        "gpt-5": "gpt-5",
        "gpt-5-mini": "gpt-5-mini",
        "gpt-5-nano": "gpt-5-nano",
        "gpt-4.1": "gpt-4.1",
        "gpt-4.1-mini": "gpt-4.1-mini",
        "gpt-4.1-nano": "gpt-4.1-nano",
    },
    "anthropic": {
        "claude-opus-4-7": "claude-opus-4-7",
        "claude-sonnet-4-6": "claude-sonnet-4-6",
        "claude-haiku-4-5": "claude-haiku-4-5",
        "claude-haiku-4-5-20251001": "claude-haiku-4-5-20251001",
    },
    "google": {
        "gemini-3-pro-preview": "gemini-3-pro-preview",
        "gemini-3-flash-preview": "gemini-3-flash-preview",
        "gemini-2.5-pro": "gemini-2.5-pro",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
        "gemini-3.1-flash-live-preview": "gemini-3.1-flash-live-preview",
        "gemini-3.1-flash-tts-preview": "gemini-3.1-flash-tts-preview",
    },
}


def default_discovery_base_url(
    provider_name: str,
    *,
    configured_base_url: str | None,
    configured_discovery_base_url: str | None,
) -> str | None:
    if configured_discovery_base_url is not None:
        return configured_discovery_base_url
    if configured_base_url is not None:
        return None
    return _DEFAULT_DISCOVERY_BASE_URLS.get(provider_name)


def openai_provider_config(config: OpenAIProviderConfig | None) -> LiteLLMProviderConfig:
    return LiteLLMProviderConfig(
        api_key=None if config is None else config.api_key,
        base_url=None if config is None else config.base_url,
        discovery_base_url=default_discovery_base_url(
            "openai",
            configured_base_url=None if config is None else config.base_url,
            configured_discovery_base_url=None if config is None else config.discovery_base_url,
        ),
        timeout_seconds=None if config is None else config.timeout_seconds,
        model_map=dict(_DEFAULT_MODEL_MAPS["openai"]),
    )


def anthropic_provider_config(config: AnthropicProviderConfig | None) -> LiteLLMProviderConfig:
    return LiteLLMProviderConfig(
        api_key=None if config is None else config.api_key,
        base_url=None if config is None else config.base_url,
        discovery_base_url=default_discovery_base_url(
            "anthropic",
            configured_base_url=None if config is None else config.base_url,
            configured_discovery_base_url=None if config is None else config.discovery_base_url,
        ),
        timeout_seconds=None if config is None else config.timeout_seconds,
        model_map=dict(_DEFAULT_MODEL_MAPS["anthropic"]),
    )


def google_provider_config(config: GoogleProviderConfig | None) -> LiteLLMProviderConfig:
    api_key = None
    auth_header = None
    auth_scheme = "bearer"
    if config is not None and config.auth is not None:
        if config.auth.method == "api_key":
            api_key = config.auth.api_key
            auth_header = "x-goog-api-key"
            auth_scheme = "token"
        elif config.auth.method == "oauth":
            api_key = config.auth.access_token
    return LiteLLMProviderConfig(
        api_key=api_key,
        base_url=None if config is None else config.base_url,
        discovery_base_url=default_discovery_base_url(
            "google",
            configured_base_url=None if config is None else config.base_url,
            configured_discovery_base_url=None if config is None else config.discovery_base_url,
        ),
        auth_header=auth_header,
        auth_scheme=auth_scheme,
        timeout_seconds=None if config is None else config.timeout_seconds,
        model_map=dict(_DEFAULT_MODEL_MAPS["google"]),
    )


def copilot_provider_config(config: CopilotProviderConfig | None) -> LiteLLMProviderConfig:
    token = None
    if config is not None and config.auth is not None:
        token = config.auth.token
    return LiteLLMProviderConfig(
        api_key=token,
        base_url=None if config is None else config.base_url,
        timeout_seconds=None if config is None else config.timeout_seconds,
    )


def litellm_provider_config(config: LiteLLMProviderConfig | None) -> LiteLLMProviderConfig:
    if config is None:
        return LiteLLMProviderConfig(discovery_base_url=_DEFAULT_DISCOVERY_BASE_URLS["litellm"])
    return LiteLLMProviderConfig(
        api_key=config.api_key,
        api_key_env_var=config.api_key_env_var,
        base_url=config.base_url,
        discovery_base_url=default_discovery_base_url(
            "litellm",
            configured_base_url=config.base_url,
            configured_discovery_base_url=config.discovery_base_url,
        ),
        auth_header=config.auth_header,
        auth_scheme=config.auth_scheme,
        ssl_verify=config.ssl_verify,
        timeout_seconds=config.timeout_seconds,
        model_map=dict(config.model_map),
    )
