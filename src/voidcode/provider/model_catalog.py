from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, ValidationError

from .config import LiteLLMProviderConfig


@dataclass(frozen=True, slots=True)
class ProviderModelMetadata:
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    supports_reasoning: bool | None = None
    supports_json_mode: bool | None = None
    cost_per_input_token: float | None = None
    cost_per_output_token: float | None = None
    cost_per_cache_read_token: float | None = None
    cost_per_cache_write_token: float | None = None
    supports_reasoning_effort: bool | None = None
    default_reasoning_effort: str | None = None
    supports_interleaved_reasoning: bool | None = None
    modalities_input: tuple[str, ...] | None = None
    modalities_output: tuple[str, ...] | None = None
    model_status: str | None = None

    def __post_init__(self) -> None:
        if self.max_input_tokens is not None or self.context_window is None:
            return
        if self.max_output_tokens is None:
            object.__setattr__(self, "max_input_tokens", self.context_window)
            return
        object.__setattr__(
            self,
            "max_input_tokens",
            max(1, self.context_window - self.max_output_tokens),
        )

    def payload(self) -> dict[str, int | bool | float | str | tuple[str, ...]]:
        payload: dict[str, int | bool | float | str | tuple[str, ...]] = {}
        if self.context_window is not None:
            payload["context_window"] = self.context_window
        if self.max_input_tokens is not None:
            payload["max_input_tokens"] = self.max_input_tokens
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        if self.supports_tools is not None:
            payload["supports_tools"] = self.supports_tools
        if self.supports_vision is not None:
            payload["supports_vision"] = self.supports_vision
        if self.supports_streaming is not None:
            payload["supports_streaming"] = self.supports_streaming
        if self.supports_reasoning is not None:
            payload["supports_reasoning"] = self.supports_reasoning
        if self.supports_json_mode is not None:
            payload["supports_json_mode"] = self.supports_json_mode
        if self.cost_per_input_token is not None:
            payload["cost_per_input_token"] = self.cost_per_input_token
        if self.cost_per_output_token is not None:
            payload["cost_per_output_token"] = self.cost_per_output_token
        if self.cost_per_cache_read_token is not None:
            payload["cost_per_cache_read_token"] = self.cost_per_cache_read_token
        if self.cost_per_cache_write_token is not None:
            payload["cost_per_cache_write_token"] = self.cost_per_cache_write_token
        if self.supports_reasoning_effort is not None:
            payload["supports_reasoning_effort"] = self.supports_reasoning_effort
        if self.default_reasoning_effort is not None:
            payload["default_reasoning_effort"] = self.default_reasoning_effort
        if self.supports_interleaved_reasoning is not None:
            payload["supports_interleaved_reasoning"] = self.supports_interleaved_reasoning
        if self.modalities_input is not None:
            payload["modalities_input"] = self.modalities_input
        if self.modalities_output is not None:
            payload["modalities_output"] = self.modalities_output
        if self.model_status is not None:
            payload["model_status"] = self.model_status
        return payload


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    provider: str
    base_url: str
    headers: dict[str, str]
    timeout_seconds: float
    api_key: str | None


type ModelCatalogFetcher = Callable[[DiscoveryRequest], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ProviderModelCatalog:
    provider: str
    models: tuple[str, ...]
    refreshed: bool
    model_metadata: dict[str, ProviderModelMetadata] = field(default_factory=dict)
    source: str = "remote"
    last_refresh_status: str = "ok"
    last_error: str | None = None
    discovery_mode: Literal[
        "configured_endpoint",
        "configured_base_url",
        "disabled",
        "unavailable",
    ] = "unavailable"


@dataclass(frozen=True, slots=True)
class ModelDiscoveryResult:
    models: tuple[str, ...]
    model_metadata: dict[str, ProviderModelMetadata]
    source: str
    last_refresh_status: str
    last_error: str | None
    discovery_mode: Literal[
        "configured_endpoint",
        "configured_base_url",
        "disabled",
        "unavailable",
    ]


@dataclass(frozen=True, slots=True)
class ModelDiscoveryPlan:
    discovery_mode: Literal[
        "configured_endpoint",
        "configured_base_url",
        "disabled",
        "unavailable",
    ]
    request: DiscoveryRequest | None
    skip_reason: str | None = None


class _DiscoveryPayloadModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _OpenAICompatibleModelItem(_DiscoveryPayloadModel):
    id: str | None = None


class _OpenAICompatibleDiscoveryPayload(_DiscoveryPayloadModel):
    data: list[str | _OpenAICompatibleModelItem] | None = None


class _AnthropicDiscoveryPayload(_DiscoveryPayloadModel):
    data: list[_OpenAICompatibleModelItem] | None = None


class _GoogleModelItem(_DiscoveryPayloadModel):
    name: str | None = None


class _GoogleDiscoveryPayload(_DiscoveryPayloadModel):
    models: list[_GoogleModelItem] | None = None


def discover_available_models(
    provider_name: str,
    config: LiteLLMProviderConfig | None,
    *,
    fetcher: ModelCatalogFetcher | None = None,
) -> ModelDiscoveryResult:
    discovered: tuple[str, ...] = ()
    error_message: str | None = None
    source = "remote"
    refresh_status = "ok"
    discovery_plan = _build_discovery_plan(provider_name=provider_name, config=config)
    if discovery_plan.request is not None:
        active_fetcher = _fetch_models if fetcher is None else fetcher
        try:
            discovered = active_fetcher(discovery_plan.request)
        except (ValueError, OSError, TimeoutError, URLError):
            discovered = ()
            source = "fallback"
            refresh_status = "failed"
            error_message = "remote model discovery failed"
    else:
        source = "fallback"
        refresh_status = "skipped"
        error_message = discovery_plan.skip_reason

    mapped_aliases = tuple(config.model_map.keys()) if config is not None else ()
    mapped_targets = tuple(config.model_map.values()) if config is not None else ()
    ordered = (*mapped_aliases, *discovered, *mapped_targets)
    deduped: list[str] = []
    seen: set[str] = set()
    for model in ordered:
        if not model or model in seen:
            continue
        seen.add(model)
        deduped.append(model)
    if source == "remote" and not discovered and deduped:
        source = "mixed"

    model_metadata = {
        model: metadata
        for model in deduped
        if (metadata := infer_model_metadata(provider_name, model)) is not None
    }

    return ModelDiscoveryResult(
        models=tuple(deduped),
        model_metadata=model_metadata,
        source=source,
        last_refresh_status=refresh_status,
        last_error=error_message,
        discovery_mode=discovery_plan.discovery_mode,
    )


def infer_model_metadata(provider_name: str, model_name: str) -> ProviderModelMetadata | None:
    provider = provider_name.strip().lower()
    model = model_name.strip().lower()
    if not model:
        return None
    if provider == "openai" or model.startswith(("gpt-5", "gpt-4.1", "gpt-4o", "o1", "o3", "o4")):
        is_reasoning = model.startswith(("gpt-5", "o1", "o3", "o4"))
        is_expensive = model.startswith(("gpt-5.5", "gpt-5.4")) and not model.startswith(
            ("gpt-5.4-mini", "gpt-5.4-nano")
        )
        is_mini_nano = model.startswith(("gpt-5.4-mini", "gpt-5.4-nano", "gpt-4o-mini"))
        common_openai: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": not model.startswith(("o1", "o3-mini")),
            "supports_streaming": not is_reasoning,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": not is_reasoning,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text",) if not model.startswith(("gpt-5", "gpt-4o")) else ("text", "image"),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if is_reasoning:
            common_openai["modalities_input"] = ("text", "image")
        if is_expensive:
            return ProviderModelMetadata(
                context_window=1_000_000,
                max_output_tokens=128_000,
                cost_per_input_token=0.000015,
                cost_per_output_token=0.000075,
                **common_openai,  # type: ignore[arg-type]
            )
        if is_mini_nano:
            return ProviderModelMetadata(
                context_window=400_000 if model.startswith(("gpt-5.4-mini", "gpt-5.4-nano")) else 128_000,
                max_output_tokens=128_000 if model.startswith(("gpt-5.4-mini", "gpt-5.4-nano")) else 16_384,
                cost_per_input_token=0.0000015,
                cost_per_output_token=0.000005,
                **common_openai,  # type: ignore[arg-type]
            )
        if model.startswith("gpt-5"):
            return ProviderModelMetadata(
                context_window=400_000,
                max_output_tokens=128_000,
                cost_per_input_token=0.00001,
                cost_per_output_token=0.00003,
                **common_openai,  # type: ignore[arg-type]
            )
        if model.startswith("gpt-4o"):
            return ProviderModelMetadata(
                context_window=128_000,
                max_output_tokens=16_384,
                cost_per_input_token=0.000005,
                cost_per_output_token=0.000015,
                **common_openai,  # type: ignore[arg-type]
            )
        if model.startswith("gpt-4.1"):
            return ProviderModelMetadata(
                context_window=1_047_576,
                max_output_tokens=32_768,
                cost_per_input_token=0.000005,
                cost_per_output_token=0.000015,
                **common_openai,  # type: ignore[arg-type]
            )
        if model.startswith(("o1", "o3", "o4")):
            return ProviderModelMetadata(
                context_window=200_000,
                max_output_tokens=100_000,
                cost_per_input_token=0.00003,
                cost_per_output_token=0.00015,
                supports_streaming=False,
                supports_json_mode=False,
                **{k: v for k, v in common_openai.items() if k not in ("supports_streaming", "supports_json_mode")},  # type: ignore[arg-type]
            )
    if provider == "anthropic" or model.startswith("claude-"):
        is_reasoning = model.startswith(("claude-opus-4", "claude-sonnet-4"))
        is_haiku = "haiku" in model
        common_anthropic: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": not is_haiku,
            "supports_streaming": True,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": False,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": is_reasoning,
            "modalities_input": ("text", "image") if not is_haiku else ("text",),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith(("claude-opus-4", "claude-sonnet-4-6")):
            return ProviderModelMetadata(
                context_window=1_000_000, max_output_tokens=64_000,
                cost_per_input_token=0.000015,
                cost_per_output_token=0.000075,
                cost_per_cache_read_token=0.000015,
                cost_per_cache_write_token=0.00001875,
                **common_anthropic,  # type: ignore[arg-type]
            )
        if model.startswith("claude-haiku-4-5"):
            return ProviderModelMetadata(
                context_window=200_000, max_output_tokens=64_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000005,
                **common_anthropic,  # type: ignore[arg-type]
            )
        return ProviderModelMetadata(
            context_window=200_000, max_output_tokens=8_192,
            cost_per_input_token=0.000003,
            cost_per_output_token=0.000015,
            **common_anthropic,  # type: ignore[arg-type]
        )
    if provider == "google" or model.startswith("gemini-"):
        is_reasoning = model.startswith(("gemini-2.5", "gemini-3"))
        is_pro = "pro" in model
        common_google: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": True,
            "supports_streaming": True,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": True,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text", "image"),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith(("gemini-3-pro", "gemini-2.5-pro")):
            return ProviderModelMetadata(
                context_window=1_048_576 if "2.5" in model else 1_000_000,
                max_output_tokens=65_536,
                cost_per_input_token=0.0000035,
                cost_per_output_token=0.0000105,
                **common_google,  # type: ignore[arg-type]
            )
        if model.startswith(("gemini-3-flash", "gemini-2.5-flash")):
            return ProviderModelMetadata(
                context_window=1_000_000,
                max_output_tokens=65_536,
                cost_per_input_token=0.000000375,
                cost_per_output_token=0.000001,
                **common_google,  # type: ignore[arg-type]
            )
        if model.startswith("gemini-3"):
            return ProviderModelMetadata(
                context_window=1_000_000, max_output_tokens=65_536,
                cost_per_input_token=0.000000375,
                cost_per_output_token=0.000001,
                **common_google,  # type: ignore[arg-type]
            )
        if model.startswith("gemini-2.5"):
            return ProviderModelMetadata(
                context_window=1_000_000, max_output_tokens=65_536,
                cost_per_input_token=0.0000015,
                cost_per_output_token=0.000005,
                **common_google,  # type: ignore[arg-type]
            )
        return ProviderModelMetadata(
            context_window=1_000_000, max_output_tokens=8_192,
            cost_per_input_token=0.0000005,
            cost_per_output_token=0.0000015,
            **common_google,  # type: ignore[arg-type]
        )
    if provider == "deepseek" or model.startswith("deepseek-"):
        is_reasoning = "reasoner" in model or model.startswith("deepseek-v4")
        common_deepseek: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": False,
            "supports_streaming": not is_reasoning,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": not is_reasoning,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text",),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith("deepseek-v4"):
            return ProviderModelMetadata(
                context_window=1_000_000, max_output_tokens=384_000,
                cost_per_input_token=0.0000027,
                cost_per_output_token=0.000011,
                supports_streaming=False,
                supports_json_mode=False,
                **{k: v for k, v in common_deepseek.items() if k not in ("supports_streaming", "supports_json_mode")},  # type: ignore[arg-type]
            )
        if model in {"deepseek-chat", "deepseek-reasoner"}:
            return ProviderModelMetadata(
                context_window=1_000_000, max_output_tokens=384_000,
                cost_per_input_token=0.0000027,
                cost_per_output_token=0.000011,
                **common_deepseek,  # type: ignore[arg-type]
            )
    if provider in {"qwen", "opencode-go"} or model.startswith(("qwen", "qwq", "qvq")):
        is_reasoning = model.startswith(("qwq", "qvq", "qwen3"))
        has_vision = model.startswith(("qvq",)) or "vl" in model
        common_qwen: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": has_vision,
            "supports_streaming": True,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": True,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text", "image") if has_vision else ("text",),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith(("qwen3.6-plus", "qwen3.6-flash", "qwen3.5-plus", "qwen3.5-flash")):
            return ProviderModelMetadata(
                context_window=1_000_000, max_output_tokens=64_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000003,
                **common_qwen,  # type: ignore[arg-type]
            )
        if model.startswith("qwen3.6-max-preview"):
            return ProviderModelMetadata(
                context_window=256_000, max_output_tokens=64_000,
                cost_per_input_token=0.000002,
                cost_per_output_token=0.000006,
                **common_qwen,  # type: ignore[arg-type]
            )
        if model.startswith("qwen3.5-"):
            return ProviderModelMetadata(
                context_window=256_000, max_output_tokens=64_000,
                cost_per_input_token=0.0000005,
                cost_per_output_token=0.0000015,
                **common_qwen,  # type: ignore[arg-type]
            )
        if model in {"qwen-plus-us", "qwen-flash-us"}:
            return ProviderModelMetadata(
                context_window=1_000_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000003,
                **common_qwen,  # type: ignore[arg-type]
            )
    if provider in {"glm", "opencode-go"} or model.startswith("glm-"):
        is_reasoning = model.startswith(("glm-5", "glm-z1"))
        has_vision = model.startswith("glm-4v") or "vision" in model
        common_glm: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": has_vision,
            "supports_streaming": True,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": True,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text", "image") if has_vision else ("text",),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith("glm-5.1"):
            return ProviderModelMetadata(
                context_window=198_000, max_output_tokens=128_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000003,
                **common_glm,  # type: ignore[arg-type]
            )
        if model.startswith("glm-5"):
            return ProviderModelMetadata(
                context_window=200_000,
                cost_per_input_token=0.0000005,
                cost_per_output_token=0.0000015,
                **common_glm,  # type: ignore[arg-type]
            )
    if provider in {"kimi", "opencode-go"} or model.startswith(("kimi-", "moonshot-")):
        is_reasoning = "thinking" in model
        common_kimi: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": False,
            "supports_streaming": True,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": True,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text",),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith(("kimi-k2.6", "kimi-k2.5", "kimi-k2-0905")):
            return ProviderModelMetadata(
                context_window=256_000, max_output_tokens=96_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000003,
                **common_kimi,  # type: ignore[arg-type]
            )
        if model.startswith(("kimi-k2-thinking", "kimi-k2-turbo")):
            return ProviderModelMetadata(
                context_window=256_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000003,
                **common_kimi,  # type: ignore[arg-type]
            )
        if model.startswith("kimi-k2-0711"):
            return ProviderModelMetadata(
                context_window=128_000,
                cost_per_input_token=0.0000005,
                cost_per_output_token=0.0000015,
                **common_kimi,  # type: ignore[arg-type]
            )
        if model.startswith("moonshot-v1-128k"):
            return ProviderModelMetadata(
                context_window=128_000,
                cost_per_input_token=0.0000012,
                cost_per_output_token=0.0000012,
                **common_kimi,  # type: ignore[arg-type]
            )
        if model.startswith("moonshot-v1-32k"):
            return ProviderModelMetadata(
                context_window=32_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000001,
                **common_kimi,  # type: ignore[arg-type]
            )
        if model.startswith("moonshot-v1-8k"):
            return ProviderModelMetadata(
                context_window=8_000,
                cost_per_input_token=0.0000008,
                cost_per_output_token=0.0000008,
                **common_kimi,  # type: ignore[arg-type]
            )
    if provider in {"minimax", "opencode-go"} or model.startswith(("minimax-", "mimo-")):
        is_reasoning = model.startswith(("minimax-m2", "mimo-v2.5"))
        has_vision = model.startswith("mimo-v2-omni")
        common_minimax: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": has_vision,
            "supports_streaming": True,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": True,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text", "image") if has_vision else ("text",),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith("minimax-m2.5"):
            return ProviderModelMetadata(
                context_window=192_000, max_output_tokens=32_000,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000003,
                **common_minimax,  # type: ignore[arg-type]
            )
        if model.startswith("minimax-m2"):
            return ProviderModelMetadata(
                context_window=204_800,
                cost_per_input_token=0.0000008,
                cost_per_output_token=0.000002,
                **common_minimax,  # type: ignore[arg-type]
            )
    if provider == "grok" or model.startswith("grok-"):
        is_reasoning = "reasoning" in model or model.startswith("grok-4")
        common_grok: dict[str, object] = {
            "supports_tools": True,
            "supports_vision": True,
            "supports_streaming": True,
            "supports_reasoning": is_reasoning,
            "supports_json_mode": True,
            "supports_reasoning_effort": is_reasoning,
            "default_reasoning_effort": "medium" if is_reasoning else None,
            "supports_interleaved_reasoning": False,
            "modalities_input": ("text", "image"),
            "modalities_output": ("text",),
            "model_status": "active",
        }
        if model.startswith(("grok-4-1-fast", "grok-4-fast")):
            return ProviderModelMetadata(
                context_window=2_000_000, max_output_tokens=30_000,
                cost_per_input_token=0.000005,
                cost_per_output_token=0.000025,
                **common_grok,  # type: ignore[arg-type]
            )
    return None


def _timeout_for_discovery(config: LiteLLMProviderConfig | None) -> float:
    if config is None or config.timeout_seconds is None:
        return 10.0
    return max(1.0, float(config.timeout_seconds))


def _headers_for_discovery(config: LiteLLMProviderConfig | None) -> dict[str, str]:
    if config is None or config.api_key is None or config.auth_scheme == "none":
        return {}
    if config.auth_scheme == "token":
        return {config.auth_header or "Authorization": config.api_key}
    header_name = config.auth_header or "Authorization"
    if header_name == "Authorization":
        return {header_name: f"Bearer {config.api_key}"}
    return {header_name: f"Bearer {config.api_key}"}


def _build_discovery_plan(
    *, provider_name: str, config: LiteLLMProviderConfig | None
) -> ModelDiscoveryPlan:
    if config is not None and config.discovery_base_url is not None:
        candidate = config.discovery_base_url.strip()
        if not candidate:
            return ModelDiscoveryPlan(
                discovery_mode="disabled",
                request=None,
                skip_reason="provider model discovery disabled by config",
            )
        return _discovery_plan_from_base_url(
            provider_name=provider_name,
            config=config,
            base_url=candidate.rstrip("/"),
            discovery_mode="configured_endpoint",
        )
    if config is not None and config.base_url:
        return _discovery_plan_from_base_url(
            provider_name=provider_name,
            config=config,
            base_url=config.base_url.rstrip("/"),
            discovery_mode="configured_base_url",
        )
    return ModelDiscoveryPlan(
        discovery_mode="unavailable",
        request=None,
        skip_reason="provider has no model discovery endpoint",
    )


def _discovery_plan_from_base_url(
    *,
    provider_name: str,
    config: LiteLLMProviderConfig | None,
    base_url: str,
    discovery_mode: Literal["configured_endpoint", "configured_base_url"],
) -> ModelDiscoveryPlan:

    provider = provider_name.strip().lower()
    timeout_seconds = _timeout_for_discovery(config)
    headers = _headers_for_discovery(config)
    if (
        provider == "google"
        and config is not None
        and config.api_key is not None
        and config.auth_scheme == "bearer"
        and config.auth_header is None
    ):
        headers = {"x-goog-api-key": config.api_key}
    if provider == "anthropic":
        api_key = None if config is None else config.api_key
        anthropic_headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
        if api_key is not None:
            anthropic_headers["x-api-key"] = api_key
        headers = anthropic_headers

    return ModelDiscoveryPlan(
        discovery_mode=discovery_mode,
        request=DiscoveryRequest(
            provider=provider,
            base_url=base_url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            api_key=None if config is None else config.api_key,
        ),
    )


def _fetch_models(request: DiscoveryRequest) -> tuple[str, ...]:
    if request.provider == "google":
        return _fetch_google_models(request)
    if request.provider == "anthropic":
        return _fetch_anthropic_models(request)
    return _fetch_openai_compatible_models(request)


def _parse_openai_compatible_discovery_payload(payload: object) -> tuple[str, ...]:
    try:
        parsed = _OpenAICompatibleDiscoveryPayload.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("provider model discovery response must be an object") from exc
    if parsed.data is None:
        return ()
    model_ids: list[str] = []
    for item in parsed.data:
        if isinstance(item, str) and item:
            model_ids.append(item)
            continue
        if isinstance(item, _OpenAICompatibleModelItem) and item.id:
            model_ids.append(item.id)
    return tuple(model_ids)


def _parse_anthropic_discovery_payload(payload: object) -> tuple[str, ...]:
    try:
        parsed = _AnthropicDiscoveryPayload.model_validate(payload)
    except ValidationError:
        return ()
    if parsed.data is None:
        return ()
    return tuple(item.id for item in parsed.data if item.id)


def _parse_google_discovery_payload(payload: object) -> tuple[str, ...]:
    try:
        parsed = _GoogleDiscoveryPayload.model_validate(payload)
    except ValidationError:
        return ()
    if parsed.models is None:
        return ()

    model_ids: list[str] = []
    for item in parsed.models:
        raw_name = item.name
        if isinstance(raw_name, str) and raw_name:
            normalized = raw_name[len("models/") :] if raw_name.startswith("models/") else raw_name
            model_ids.append(normalized)
    return tuple(model_ids)


def _fetch_openai_compatible_models(
    request: DiscoveryRequest,
) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1/models"):
        models_url = base_url
    elif re.search(r"/v[0-9]+(?:beta|alpha)?$", base_url, re.IGNORECASE):
        models_url = f"{base_url}/models"
    elif base_url.endswith("/v1"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1/models"

    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_openai_compatible_discovery_payload(payload)


def _fetch_anthropic_models(request: DiscoveryRequest) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    models_url = f"{base_url}/models" if base_url.endswith("/v1") else f"{base_url}/v1/models"
    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_anthropic_discovery_payload(payload)


def _fetch_google_models(request: DiscoveryRequest) -> tuple[str, ...]:
    base_url = request.base_url.rstrip("/")
    if base_url.endswith("/v1beta/models"):
        models_url = base_url
    elif base_url.endswith("/v1beta"):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1beta/models"

    uses_google_api_key_header = any(
        header_name.lower() == "x-goog-api-key" for header_name in request.headers
    )
    uses_authorization_header = any(
        header_name.lower() == "authorization" for header_name in request.headers
    )
    if (
        request.api_key is not None
        and not uses_authorization_header
        and not uses_google_api_key_header
    ):
        models_url = f"{models_url}?key={quote(request.api_key, safe='')}"

    http_request = Request(url=models_url, headers=request.headers, method="GET")
    with urlopen(http_request, timeout=request.timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))

    return _parse_google_discovery_payload(payload)
