from __future__ import annotations

from dataclasses import dataclass

from .config import SimplifiedProviderConfig, simplified_config_to_litellm
from .litellm_backend import LiteLLMBackendSingleAgentProvider
from .protocol import SingleAgentProvider


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
            api_key: "your-api-key"  # or set OPENCODE_GO_API_KEY env var
            model_map:
              glm-5: glm-5  # optional model alias

    Environment Variables:
        OPENCODE_GO_API_KEY: API key for OpenCode Go authentication

    Note:
        OpenCode Go uses different endpoints for different model families:
        - OpenAI-compatible: https://opencode.ai/zen/go/v1/chat/completions
        - Anthropic-compatible: https://opencode.ai/zen/go/v1/messages
        Model IDs in config use format: opencode-go/<model-id>
    """

    name: str = "opencode-go"
    config: SimplifiedProviderConfig | None = None

    def single_agent_provider(self) -> SingleAgentProvider:
        adapted_config = simplified_config_to_litellm(self.name, self.config)
        return LiteLLMBackendSingleAgentProvider(name=self.name, config=adapted_config)
