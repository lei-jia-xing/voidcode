from __future__ import annotations

from .anthropic_native import AnthropicMessagesProvider, AnthropicMessagesTransport, AnthropicTransport
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
    OpenAICompatibleProviderConfig,
    ProviderConfigs,
    ProviderEndpointConfig,
    ProviderFallbackConfig,
    parse_provider_configs_payload,
    parse_provider_fallback_payload,
    serialize_provider_configs,
    serialize_provider_fallback_config,
)
from .copilot import CopilotModelProvider
from .endpoint import OpenAIEndpointProvider
from .errors import (
    ProviderContextLimitError,
    ProviderError,
    classify_provider_error,
    format_invalid_provider_config_error,
)
from .google import GoogleModelProvider
from .model_catalog import (
    ProviderModelCatalog,
    ProviderModelMetadata,
    discover_available_models,
)
from .models import (
    ProviderModelSelection,
    ProviderResolutionMetadata,
    ResolvedProviderChain,
    ResolvedProviderConfig,
    ResolvedProviderModel,
)
from .openai import OpenAIModelProvider
from .openai_native import OpenAIChatCompletionsProvider, OpenAIChatCompletionsTransport, OpenAITransport
from .opencode import OpenCodeModelProvider
from .opencode_go import OpenCodeGoModelProvider
from .openrouter import OpenRouterModelProvider
from .protocol import (
    ModelTurnProvider,
    ProviderExecutionError,
    ProviderTokenUsage,
    ProviderTransport,
    ProviderTurnRequest,
    ProviderTurnResult,
    TurnProvider,
)
from .registry import (
    AnthropicCompatibleModelProvider,
    ModelProviderRegistry,
    OpenAICompatibleModelProvider,
)
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
    "AnthropicMessagesProvider",
    "AnthropicMessagesTransport",
    "AnthropicTransport",
    "AnthropicCompatibleModelProvider",
    "CopilotModelProvider",
    "GoogleModelProvider",
    "OpenAIEndpointProvider",
    "ModelTurnProvider",
    "ProviderModelCatalog",
    "ProviderModelMetadata",
    "ModelProviderRegistry",
    "OpenRouterModelProvider",
    "OpenAIChatCompletionsProvider",
    "OpenAIChatCompletionsTransport",
    "OpenAITransport",
    "OpenAIModelProvider",
    "OpenAICompatibleModelProvider",
    "OpenCodeModelProvider",
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
    "ProviderTransport",
    "ProviderTokenUsage",
    "ProviderFallbackConfig",
    "ProviderModelSelection",
    "ProviderResolutionMetadata",
    "ResolvedProviderChain",
    "ResolvedProviderConfig",
    "ResolvedProviderModel",
    "TurnProvider",
    "ProviderTurnRequest",
    "ProviderTurnResult",
    "ProviderContextLimitError",
    "ProviderError",
    "OpenAICompatibleProviderConfig",
    "ProviderEndpointConfig",
    "OpenCodeGoModelProvider",
    "classify_provider_error",
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
