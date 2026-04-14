from __future__ import annotations

from dataclasses import dataclass

from .anthropic import AnthropicModelProvider
from .config import ProviderConfigs
from .copilot import CopilotModelProvider
from .google import GoogleModelProvider
from .litellm import LiteLLMModelProvider
from .openai import OpenAIModelProvider
from .protocol import ModelProvider, SingleAgentProvider, StubSingleAgentProvider


@dataclass(frozen=True, slots=True)
class StaticModelProvider:
    name: str

    def single_agent_provider(self) -> SingleAgentProvider:
        return StubSingleAgentProvider(name=self.name)


@dataclass(slots=True)
class ModelProviderRegistry:
    providers: dict[str, ModelProvider]

    @classmethod
    def with_defaults(
        cls, *, provider_configs: ProviderConfigs | None = None
    ) -> ModelProviderRegistry:
        configs = provider_configs or ProviderConfigs()
        return cls(
            providers={
                "opencode": StaticModelProvider(name="opencode"),
                "openai": OpenAIModelProvider(config=configs.openai),
                "anthropic": AnthropicModelProvider(config=configs.anthropic),
                "google": GoogleModelProvider(config=configs.google),
                "copilot": CopilotModelProvider(config=configs.copilot),
                "litellm": LiteLLMModelProvider(config=configs.litellm),
            }
        )

    def resolve(self, provider_name: str) -> ModelProvider:
        return self.providers.get(provider_name, StaticModelProvider(name=provider_name))
