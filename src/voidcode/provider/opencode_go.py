from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .model_catalog import ToolFeedbackMode
from .protocol import ProviderTurnRequest, TurnProvider

# Only minimax-m2.5 rides the Anthropic-compatible endpoint
# (https://opencode.ai/zen/go/v1/messages). Everything else — including
# minimax-m2.7, qwen3.5-plus, qwen3.6-plus — goes through the
# OpenAI-compatible gateway endpoint (https://opencode.ai/zen/go/v1/chat/completions).
_ANTHROPIC_COMPATIBLE_MODELS = frozenset({"minimax-m2.5"})

_TOOL_FEEDBACK_OVERRIDES: dict[str, ToolFeedbackMode] = {
    "qwen3.5-plus": "synthetic_user_message",
    "qwen3.6-plus": "synthetic_user_message",
    "minimax-m2.5": "synthetic_user_message",
    "minimax-m2.7": "synthetic_user_message",
}


@dataclass(frozen=True, slots=True)
class OpenCodeGoSingleAgentProvider(LiteLLMBackendSingleAgentProvider):
    """LiteLLM adapter for OpenCode Go's gateway.

    All models except minimax-m2.5 are routed to the OpenAI-compatible
    endpoint; only minimax-m2.5 uses the Anthropic-compatible endpoint.
    """

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
        OpenCode Go routes models through two gateway endpoints:
        - OpenAI-compatible (default, used by minimax-m2.7, qwen3.5-plus,
          qwen3.6-plus, and everything else):
          https://opencode.ai/zen/go/v1/chat/completions
        - Anthropic-compatible (minimax-m2.5 only):
          https://opencode.ai/zen/go/v1/messages
        Model IDs in config use format: opencode-go/<model-id>
    """

    name: str = "opencode-go"
    config: SimplifiedProviderConfig | None = None

    def provider_config(self):
        return simplified_config_to_litellm(self.name, self.config)

    def turn_provider(self) -> TurnProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return OpenCodeGoSingleAgentProvider(
            name=self.name,
            config=adapted_config,
            use_raw_model_name=True,
            tool_feedback_model_overrides=_TOOL_FEEDBACK_OVERRIDES,
        )
