from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from .config import RuntimeAgentConfig, RuntimeProviderFallbackConfig

__all__ = [
    "CALLABLE_SUBAGENT_PRESETS",
    "ResolvedSubagentRoute",
    "SubagentExecutablePreset",
    "SubagentExecutionMode",
    "SubagentRoutingIdentity",
    "delegated_model_for_route_from_configs",
    "parse_subagent_routing_identity",
    "provider_fallback_for_agent_selection",
    "provider_fallback_with_preferred_model",
    "resolve_subagent_route",
    "subagent_routing_identity_from_metadata",
]


def delegated_model_for_route_from_configs(
    *,
    selected_preset: str,
    request_agent: RuntimeAgentConfig | None,
    agents: Mapping[str, RuntimeAgentConfig],
    base_model: str | None,
) -> str | None:
    if request_agent is not None and request_agent.model is not None:
        return request_agent.model
    preset_agent = agents.get(selected_preset)
    if preset_agent is not None and preset_agent.model is not None:
        return preset_agent.model
    return base_model


def provider_fallback_with_preferred_model(
    provider_fallback: RuntimeProviderFallbackConfig,
    preferred_model: str,
) -> RuntimeProviderFallbackConfig:
    from .config import RuntimeProviderFallbackConfig

    return RuntimeProviderFallbackConfig(
        preferred_model=preferred_model,
        fallback_models=tuple(fallback_model for fallback_model in provider_fallback.fallback_models if fallback_model != preferred_model),
    )


def provider_fallback_for_agent_selection(
    *,
    model: str | None,
    preset_agent: RuntimeAgentConfig | None,
    base_provider_fallback: RuntimeProviderFallbackConfig | None,
) -> RuntimeProviderFallbackConfig | None:
    if preset_agent is not None and preset_agent.provider_fallback is not None:
        if model is None or model == preset_agent.provider_fallback.preferred_model:
            return preset_agent.provider_fallback
        return provider_fallback_with_preferred_model(
            preset_agent.provider_fallback,
            model,
        )
    if base_provider_fallback is None:
        return None
    if model is None or model == base_provider_fallback.preferred_model:
        return base_provider_fallback
    return provider_fallback_with_preferred_model(base_provider_fallback, model)


type SubagentExecutionMode = Literal["sync", "background"]
type SubagentExecutablePreset = str


def _parse_subagent_routing_mode(value: object) -> SubagentExecutionMode:
    if value == "sync":
        return "sync"
    if value == "background":
        return "background"
    raise ValueError("delegation metadata mode must be 'sync' or 'background'")


def _normalized_optional_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class SubagentRoutingIdentity:
    mode: SubagentExecutionMode
    subagent_type: str
    description: str | None = None
    command: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedSubagentRoute:
    requested: SubagentRoutingIdentity
    selected_preset: SubagentExecutablePreset
    execution_engine: Literal["provider"] = "provider"

    @property
    def selected_identity(self) -> dict[str, object]:
        return {
            "preset": self.selected_preset,
            "mode": "subagent",
            "requested_mode": self.requested.mode,
            **({"requested_subagent_type": self.requested.subagent_type} if self.requested.subagent_type is not None else {}),
            **({"description": self.requested.description} if self.requested.description is not None else {}),
            **({"command": self.requested.command} if self.requested.command is not None else {}),
        }


CALLABLE_SUBAGENT_PRESETS: tuple[str, ...] = ("advisor", "explore", "researcher", "worker", "product")


def resolve_subagent_route(
    requested: SubagentRoutingIdentity,
    *,
    callable_subagent_presets: frozenset[str] | tuple[str, ...] | None = None,
) -> ResolvedSubagentRoute:
    callable_presets = callable_subagent_presets or CALLABLE_SUBAGENT_PRESETS
    if requested.subagent_type == "leader":
        raise ValueError("subagent_type 'leader' is not a callable child preset")
    if requested.subagent_type not in callable_presets:
        valid_presets = ", ".join(sorted(callable_presets))
        raise ValueError(f"unknown subagent_type '{requested.subagent_type}'; valid child presets are: {valid_presets}")
    return ResolvedSubagentRoute(requested=requested, selected_preset=requested.subagent_type)


def subagent_routing_identity_from_metadata(
    metadata: Mapping[str, object] | None,
) -> SubagentRoutingIdentity | None:
    if metadata is None:
        return None
    raw_routing = metadata.get("delegation")
    if raw_routing is None:
        return None
    return parse_subagent_routing_identity(raw_routing)


def parse_subagent_routing_identity(metadata: object) -> SubagentRoutingIdentity:
    """Parse the shared identity fields from delegation metadata."""
    if not isinstance(metadata, Mapping):
        raise ValueError("delegation metadata must be an object")

    routing_items = cast(dict[object, object], metadata)
    non_string_keys = sorted(repr(key) for key in routing_items if not isinstance(key, str))
    if non_string_keys:
        joined = ", ".join(non_string_keys)
        raise ValueError(f"delegation metadata keys must be strings; received invalid key(s): {joined}")

    routing_metadata: dict[str, object] = {key: value for key, value in routing_items.items() if isinstance(key, str)}
    mode = _parse_subagent_routing_mode(routing_metadata.get("mode"))
    subagent_type = routing_metadata.get("subagent_type")
    if subagent_type is None:
        raise ValueError("delegation.subagent_type is required")
    normalized_subagent_type = _normalized_optional_string(subagent_type, field_name="delegation.subagent_type")
    description = routing_metadata.get("description")
    command = routing_metadata.get("command")
    return SubagentRoutingIdentity(
        mode=mode,
        subagent_type=normalized_subagent_type,
        description=(_normalized_optional_string(description, field_name="delegation.description") if description is not None else None),
        command=(_normalized_optional_string(command, field_name="delegation.command") if command is not None else None),
    )
