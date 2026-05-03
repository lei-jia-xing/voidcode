from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import ProviderTurnRequest, TurnProvider

_ANTHROPIC_COMPATIBLE_MODELS = frozenset({"minimax-m2.5", "minimax-m2.7"})
_ALIBABA_COMPATIBLE_MODELS = frozenset({"qwen3.5-plus", "qwen3.6-plus"})


@dataclass(frozen=True, slots=True)
class OpenCodeGoSingleAgentProvider(LiteLLMBackendSingleAgentProvider):
    """LiteLLM adapter for OpenCode Go's model-family-specific SDK routes."""

    def _completion_kwargs_for_request(self, request: ProviderTurnRequest) -> dict[str, object]:
        kwargs = LiteLLMBackendSingleAgentProvider._completion_kwargs_for_request(self, request)
        model_name = self._mapped_model_name_for_request(request)
        if model_name in _ANTHROPIC_COMPATIBLE_MODELS:
            kwargs["custom_llm_provider"] = "anthropic"
            kwargs["api_base"] = "https://opencode.ai/zen/go"
            kwargs["extra_headers"] = {
                "anthropic-version": "2023-06-01",
                "user-agent": "@ai-sdk/anthropic",
            }
            return kwargs
        if model_name in _ALIBABA_COMPATIBLE_MODELS:
            kwargs["custom_llm_provider"] = "dashscope"
            return kwargs
        kwargs["custom_llm_provider"] = "openai"
        return kwargs


@dataclass(frozen=True, slots=True)
class OpenCodeGoModelProvider:
    """OpenCode Go Model Provider.

    OpenCode Go provides unified access to multiple Chinese AI models through
    a single subscription at https://opencode.ai

    Supported models: GLM-5/5.1, Kimi K2.5/2.6, MiniMax M2.5/M2.7,
    Qwen3.5+/3.6+, MiMo v2 (Pro/Omni)

    Usage:
        providers:
          opencode-go:
            api_key: "your-api-key"  # or set OPENCODE_API_KEY env var
            model_map:
              glm-5: glm-5  # optional model alias

    Environment Variables:
        OPENCODE_API_KEY: API key shared by OpenCode Zen and OpenCode Go

    Note:
        OpenCode Go uses different endpoints for different model families:
        - OpenAI-compatible: https://opencode.ai/zen/go/v1/chat/completions
        - Anthropic-compatible: https://opencode.ai/zen/go/v1/messages
        Model IDs in config use format: opencode-go/<model-id>
    """

    name: str = "opencode-go"
    config: SimplifiedProviderConfig | None = None

    def turn_provider(self) -> TurnProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return OpenCodeGoSingleAgentProvider(
            name=self.name,
            config=adapted_config,
            use_raw_model_name=True,
        )
