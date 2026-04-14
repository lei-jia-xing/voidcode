from __future__ import annotations

from .anthropic import AnthropicModelProvider
from .auth import (
    ProviderAuthAuthorizeRequest,
    ProviderAuthAuthorizeResult,
    ProviderAuthCallback,
    ProviderAuthCallbackRequest,
    ProviderAuthMaterial,
    ProviderAuthMethod,
    ProviderAuthMethodsResponse,
    ProviderAuthResolutionError,
    ProviderAuthResolver,
    provider_auth_error_to_execution_kind,
)
from .config import (
    LiteLLMProviderConfig,
    ProviderConfigs,
    ProviderFallbackConfig,
    parse_provider_configs_payload,
    parse_provider_fallback_payload,
    serialize_provider_configs,
    serialize_provider_fallback_config,
)
from .copilot import CopilotModelProvider
from .errors import (
    SingleAgentContextLimitError,
    SingleAgentProviderError,
    classify_provider_error,
    format_fallback_exhausted_error,
    format_invalid_provider_config_error,
)
from .google import GoogleModelProvider
from .litellm import LiteLLMModelProvider
from .model_catalog import ProviderModelCatalog, discover_available_models
from .models import (
    ProviderModelSelection,
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from .openai import OpenAIModelProvider
from .protocol import (
    ModelProvider,
    ProviderExecutionError,
    SingleAgentProvider,
    SingleAgentTurnRequest,
    SingleAgentTurnResult,
    StubSingleAgentProvider,
)
from .registry import ModelProviderRegistry, StaticModelProvider
from .resolution import (
    resolve_provider_chain,
    resolve_provider_config,
    resolve_provider_model,
)
from .snapshot import (
    parse_resolved_provider_snapshot,
    resolved_provider_snapshot,
)

__all__ = [
    "AnthropicModelProvider",
    "CopilotModelProvider",
    "GoogleModelProvider",
    "LiteLLMModelProvider",
    "ModelProvider",
    "ProviderModelCatalog",
    "ModelProviderRegistry",
    "OpenAIModelProvider",
    "ProviderAuthAuthorizeRequest",
    "ProviderAuthAuthorizeResult",
    "ProviderAuthCallback",
    "ProviderAuthCallbackRequest",
    "ProviderAuthMaterial",
    "ProviderAuthMethod",
    "ProviderAuthMethodsResponse",
    "ProviderAuthResolutionError",
    "ProviderAuthResolver",
    "ProviderConfigs",
    "ProviderExecutionError",
    "ProviderFallbackConfig",
    "ProviderModelSelection",
    "ResolvedProviderChain",
    "ResolvedProviderConfig",
    "ResolvedProviderModel",
    "SingleAgentProvider",
    "SingleAgentTurnRequest",
    "SingleAgentTurnResult",
    "SingleAgentContextLimitError",
    "SingleAgentProviderError",
    "LiteLLMProviderConfig",
    "StaticModelProvider",
    "StubSingleAgentProvider",
    "classify_provider_error",
    "format_fallback_exhausted_error",
    "format_invalid_provider_config_error",
    "parse_resolved_provider_snapshot",
    "parse_provider_configs_payload",
    "parse_provider_fallback_payload",
    "provider_auth_error_to_execution_kind",
    "resolve_provider_chain",
    "resolve_provider_config",
    "resolve_provider_model",
    "resolved_provider_snapshot",
    "discover_available_models",
    "serialize_provider_configs",
    "serialize_provider_fallback_config",
]
