from __future__ import annotations

from dataclasses import dataclass

from .config import LiteLLMProviderConfig
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import TurnProvider

_OPENCODE_ZEN_BASE_URL = "https://opencode.ai/zen/v1"
_OPENCODE_ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
_OPENCODE_API_KEY_ENV_VAR = "OPENCODE_API_KEY"


@dataclass(frozen=True, slots=True)
class OpenCodeModelProvider:
    name: str = "opencode"
    config: LiteLLMProviderConfig | None = None

    def provider_config(self) -> LiteLLMProviderConfig:
        if self.config is None:
            return LiteLLMProviderConfig(
                base_url=_OPENCODE_ZEN_BASE_URL,
                discovery_base_url=_OPENCODE_ZEN_MODELS_URL,
                api_key_env_var=_OPENCODE_API_KEY_ENV_VAR,
                model_map={},
            )
        return LiteLLMProviderConfig(
            api_key=self.config.api_key,
            api_key_env_var=self.config.api_key_env_var,
            base_url=self.config.base_url or _OPENCODE_ZEN_BASE_URL,
            discovery_base_url=(
                self.config.discovery_base_url
                if self.config.discovery_base_url is not None
                else _OPENCODE_ZEN_MODELS_URL
            ),
            auth_header=self.config.auth_header,
            auth_scheme=self.config.auth_scheme,
            auth_scheme_explicit=self.config.auth_scheme_explicit,
            ssl_verify=self.config.ssl_verify,
            timeout_seconds=self.config.timeout_seconds,
            model_map=(dict(self.config.model_map) if self.config.model_map else {}),
            transient_retry=self.config.transient_retry,
        )

    def turn_provider(self) -> TurnProvider:
        return LiteLLMBackendSingleAgentProvider(
            name=self.name,
            config=self.provider_config(),
            use_raw_model_name=True,
        )
