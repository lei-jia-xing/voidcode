from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from ..provider.errors import format_invalid_provider_config_error
from ..provider.models import ResolvedProviderConfig
from .config import (
    ExecutionEngineName,
    ExternalDirectoryPermissionConfig,
    RuntimeAgentConfig,
    RuntimeContextWindowConfig,
    RuntimeProviderFallbackConfig,
    RuntimeProvidersConfig,
    RuntimeToolsConfig,
    parse_provider_configs_payload,
    parse_provider_fallback_payload,
    parse_runtime_context_window_payload,
    parse_runtime_policy_payload,
    parse_runtime_tools_payload,
    serialize_provider_configs,
    serialize_runtime_agent_config,
    serialize_runtime_context_window_config,
    serialize_runtime_tools_config,
)
from .permission import (
    ExternalDirectoryPolicy,
    PatternPermissionRule,
    PermissionDecision,
)
from .permission_policy import permission_decision_or_none
from .policy import serialize_runtime_policy_config

PERSISTED_RUNTIME_CONFIG_KEYS = frozenset(
    {
        "approval_mode",
        "permission",
        "policy",
        "execution_engine",
        "max_steps",
        "tool_timeout_seconds",
        "reasoning_effort",
        "model",
        "fallback_models",
        "providers",
        "resolved_provider",
        "resolved_hook_presets",
        "tools",
        "agent",
        "agents",
        "categories",
        "context_window",
        "lsp",
        "mcp",
        "workflow",
    }
)


@dataclass(frozen=True, slots=True)
class EffectiveRuntimeConfig:
    approval_mode: PermissionDecision
    permission: ExternalDirectoryPermissionConfig
    model: str | None
    execution_engine: ExecutionEngineName
    max_steps: int | None
    tool_timeout_seconds: int | None = None
    reasoning_effort: str | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    providers: RuntimeProvidersConfig | None = None
    resolved_provider: ResolvedProviderConfig = field(default_factory=ResolvedProviderConfig)
    agent: RuntimeAgentConfig | None = None
    context_window: RuntimeContextWindowConfig | None = None
    tools: RuntimeToolsConfig | None = None
    policy: object | None = None


@dataclass(frozen=True, slots=True)
class PersistedRuntimeConfigMaterialization:
    approval_mode: PermissionDecision
    permission: ExternalDirectoryPermissionConfig
    policy: object | None
    model: str | None
    execution_engine: ExecutionEngineName
    max_steps: int | None
    tool_timeout_seconds: int | None
    reasoning_effort: str | None
    providers: RuntimeProvidersConfig | None
    provider_fallback: RuntimeProviderFallbackConfig | None
    tools: RuntimeToolsConfig | None
    raw_agent: object | None
    has_agent: bool
    context_window: RuntimeContextWindowConfig | None
    raw_resolved_provider: object | None


def serialize_runtime_config_core(config: EffectiveRuntimeConfig) -> dict[str, object]:
    runtime_config_metadata: dict[str, object] = {
        "approval_mode": config.approval_mode,
        "permission": serialize_external_permission_config(config.permission),
        "execution_engine": config.execution_engine,
        "max_steps": config.max_steps,
        "tool_timeout_seconds": config.tool_timeout_seconds,
        "fallback_models": (
            list(config.provider_fallback.fallback_models) if config.provider_fallback is not None and config.model is not None else []
        ),
    }
    serialized_policy = serialize_runtime_policy_config(config.policy)
    if serialized_policy is not None:
        runtime_config_metadata["policy"] = serialized_policy
    serialized_providers = serialize_provider_configs(config.providers)
    serialized_runtime_providers = runtime_provider_config_metadata(serialized_providers)
    if serialized_runtime_providers:
        runtime_config_metadata["providers"] = serialized_runtime_providers
    serialized_context_window = serialize_runtime_context_window_config(config.context_window)
    if serialized_context_window is not None:
        runtime_config_metadata["context_window"] = serialized_context_window
    if config.model is not None:
        runtime_config_metadata["model"] = config.model
    if config.reasoning_effort is not None:
        runtime_config_metadata["reasoning_effort"] = config.reasoning_effort
    serialized_tools = serialize_runtime_tools_config(config.tools)
    if serialized_tools is not None:
        runtime_config_metadata["tools"] = serialized_tools
    serialized_agent = serialize_runtime_agent_config(config.agent)
    if serialized_agent is not None:
        runtime_config_metadata["agent"] = serialized_agent
    return runtime_config_metadata


def parse_persisted_runtime_config(
    runtime_config: Mapping[str, object],
    *,
    allow_legacy_permission_scopes: bool = False,
) -> PersistedRuntimeConfigMaterialization:
    unknown_runtime_config_keys = sorted(key for key in runtime_config if key not in PERSISTED_RUNTIME_CONFIG_KEYS)
    if unknown_runtime_config_keys:
        raise ValueError(f"persisted runtime_config field '{unknown_runtime_config_keys[0]}' is not supported")
    required_runtime_config_keys = {
        "approval_mode",
        "permission",
        "execution_engine",
        "max_steps",
        "tool_timeout_seconds",
        "fallback_models",
    }
    missing_runtime_config_keys = sorted(required_runtime_config_keys - runtime_config.keys())
    if missing_runtime_config_keys:
        raise ValueError("persisted runtime_config is missing required field(s): " + ", ".join(missing_runtime_config_keys))

    approval_mode = permission_decision_or_none(runtime_config["approval_mode"])
    if approval_mode is None:
        raise ValueError("persisted runtime_config approval_mode is invalid")

    permission = parse_persisted_external_permission_config(
        runtime_config["permission"],
        allow_missing_scopes=allow_legacy_permission_scopes,
    )

    policy = None
    if "policy" in runtime_config:
        policy = parse_runtime_policy_payload(
            runtime_config.get("policy"),
            source="persisted runtime_config.policy",
        )

    model = None
    persisted_model = runtime_config.get("model")
    if persisted_model is None or isinstance(persisted_model, str):
        model = persisted_model
    else:
        raise ValueError("persisted runtime_config model must be a string or null")

    persisted_max_steps = runtime_config["max_steps"]
    if persisted_max_steps is None:
        max_steps = None
    elif isinstance(persisted_max_steps, int) and not isinstance(persisted_max_steps, bool):
        if persisted_max_steps < 0:
            raise ValueError("persisted runtime_config max_steps must be a non-negative integer (0 = unlimited)")
        max_steps = persisted_max_steps
    else:
        raise ValueError("persisted runtime_config max_steps must be an integer or null")

    persisted_tool_timeout = runtime_config["tool_timeout_seconds"]
    if persisted_tool_timeout is None:
        tool_timeout_seconds = None
    elif isinstance(persisted_tool_timeout, int) and not isinstance(persisted_tool_timeout, bool):
        if persisted_tool_timeout < 1:
            raise ValueError("persisted runtime_config tool_timeout_seconds must be at least 1")
        tool_timeout_seconds = persisted_tool_timeout
    else:
        raise ValueError("persisted runtime_config tool_timeout_seconds must be an integer or null")

    reasoning_effort = None
    if "reasoning_effort" in runtime_config:
        persisted_reasoning_effort = runtime_config.get("reasoning_effort")
        if persisted_reasoning_effort is None:
            reasoning_effort = None
        elif isinstance(persisted_reasoning_effort, str) and persisted_reasoning_effort:
            reasoning_effort = persisted_reasoning_effort
        else:
            raise ValueError("persisted runtime_config reasoning_effort must be a non-empty string")

    providers = None
    if "providers" in runtime_config:
        try:
            providers = parse_provider_configs_payload(
                runtime_config.get("providers"),
                source="persisted runtime_config.providers",
            )
        except ValueError as exc:
            raise ValueError(
                format_invalid_provider_config_error(
                    "persisted runtime_config.providers",
                    str(exc),
                )
            ) from exc

    provider_fallback = parse_persisted_provider_fallback(runtime_config, model=model)

    tools = None
    if "tools" in runtime_config:
        tools = parse_persisted_runtime_tools_config(runtime_config.get("tools"))

    context_window = None
    if "context_window" in runtime_config:
        try:
            context_window = parse_runtime_context_window_payload(
                runtime_config.get("context_window"),
                source="persisted runtime_config.context_window",
            )
        except ValueError as exc:
            raise ValueError(
                format_invalid_provider_config_error(
                    "persisted runtime_config.context_window",
                    str(exc),
                )
            ) from exc

    execution_engine = execution_engine_or_none(runtime_config["execution_engine"])
    if execution_engine is None:
        raise ValueError("persisted runtime_config execution_engine is invalid")

    return PersistedRuntimeConfigMaterialization(
        approval_mode=approval_mode,
        permission=permission,
        policy=policy,
        model=model,
        execution_engine=execution_engine,
        max_steps=max_steps,
        tool_timeout_seconds=tool_timeout_seconds,
        reasoning_effort=reasoning_effort,
        providers=providers,
        provider_fallback=provider_fallback,
        tools=tools,
        raw_agent=runtime_config.get("agent"),
        has_agent="agent" in runtime_config,
        context_window=context_window,
        raw_resolved_provider=runtime_config.get("resolved_provider"),
    )


def parse_persisted_provider_fallback(
    runtime_config: Mapping[str, object],
    *,
    model: str | None,
) -> RuntimeProviderFallbackConfig | None:
    if "fallback_models" not in runtime_config:
        raise ValueError("persisted runtime_config.fallback_models is required")
    raw_fallback_models = runtime_config["fallback_models"]
    if raw_fallback_models == []:
        return None
    if model is None:
        raise ValueError("persisted runtime_config.model is required when fallback_models are present")
    try:
        return parse_provider_fallback_payload(
            {
                "preferred_model": model,
                "fallback_models": raw_fallback_models,
            },
            source="persisted runtime_config.fallback_models",
        )
    except ValueError as exc:
        raise ValueError(
            format_invalid_provider_config_error(
                "persisted runtime_config.fallback_models",
                str(exc),
            )
        ) from exc


def apply_request_runtime_config_overrides(
    resolved: EffectiveRuntimeConfig,
    *,
    max_steps: int | None,
    reasoning_effort: object,
    context_transform_refs: tuple[str, ...] | None,
) -> EffectiveRuntimeConfig:
    if max_steps is not None:
        resolved = EffectiveRuntimeConfig(
            approval_mode=resolved.approval_mode,
            permission=resolved.permission,
            model=resolved.model,
            execution_engine=resolved.execution_engine,
            max_steps=max_steps,
            tool_timeout_seconds=resolved.tool_timeout_seconds,
            reasoning_effort=resolved.reasoning_effort,
            provider_fallback=resolved.provider_fallback,
            providers=resolved.providers,
            resolved_provider=resolved.resolved_provider,
            agent=resolved.agent,
            context_window=resolved.context_window,
            tools=resolved.tools,
            policy=resolved.policy,
        )
    if isinstance(reasoning_effort, str) and reasoning_effort:
        resolved = EffectiveRuntimeConfig(
            approval_mode=resolved.approval_mode,
            permission=resolved.permission,
            model=resolved.model,
            execution_engine=resolved.execution_engine,
            max_steps=resolved.max_steps,
            tool_timeout_seconds=resolved.tool_timeout_seconds,
            reasoning_effort=reasoning_effort,
            provider_fallback=resolved.provider_fallback,
            providers=resolved.providers,
            resolved_provider=resolved.resolved_provider,
            agent=resolved.agent,
            context_window=resolved.context_window,
            tools=resolved.tools,
            policy=resolved.policy,
        )
    if context_transform_refs is not None:
        resolved = EffectiveRuntimeConfig(
            approval_mode=resolved.approval_mode,
            permission=resolved.permission,
            model=resolved.model,
            execution_engine=resolved.execution_engine,
            max_steps=resolved.max_steps,
            tool_timeout_seconds=resolved.tool_timeout_seconds,
            reasoning_effort=resolved.reasoning_effort,
            provider_fallback=resolved.provider_fallback,
            providers=resolved.providers,
            resolved_provider=resolved.resolved_provider,
            agent=(
                RuntimeAgentConfig(
                    preset=resolved.agent.preset,
                    prompt_profile=resolved.agent.prompt_profile,
                    prompt=resolved.agent.prompt,
                    prompt_append=resolved.agent.prompt_append,
                    prompt_ref=resolved.agent.prompt_ref,
                    prompt_source=resolved.agent.prompt_source,
                    prompt_materialization=resolved.agent.prompt_materialization,
                    manifest_source_scope=resolved.agent.manifest_source_scope,
                    manifest_source_path=resolved.agent.manifest_source_path,
                    manifest_tool_allowlist=resolved.agent.manifest_tool_allowlist,
                    manifest_skill_refs=resolved.agent.manifest_skill_refs,
                    manifest_hook_refs=resolved.agent.manifest_hook_refs,
                    hook_refs=resolved.agent.hook_refs,
                    context_transform_refs=context_transform_refs,
                    model=resolved.agent.model,
                    execution_engine=resolved.agent.execution_engine,
                    tools=resolved.agent.tools,
                    skills=resolved.agent.skills,
                    mcp_binding=resolved.agent.mcp_binding,
                    provider_fallback=resolved.agent.provider_fallback,
                )
                if resolved.agent is not None
                else None
            ),
            context_window=resolved.context_window,
            tools=resolved.tools,
            policy=resolved.policy,
        )
    return resolved


def serialize_external_permission_config(
    permission: ExternalDirectoryPermissionConfig,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "external_directory_read": dict(permission.read.rules),
        "external_directory_write": dict(permission.write.rules),
    }
    if permission.rules:
        payload["rules"] = [
            {
                key: value
                for key, value in {
                    "tool": rule.tool,
                    "path": rule.path,
                    "command": rule.command,
                    "decision": rule.decision,
                }.items()
                if value is not None
            }
            for rule in permission.rules
        ]
    return payload


def parse_persisted_external_permission_config(
    raw_permission: object,
    *,
    allow_missing_scopes: bool = False,
) -> ExternalDirectoryPermissionConfig:
    if not isinstance(raw_permission, dict):
        raise ValueError("persisted runtime_config permission must be an object")
    payload = cast(dict[object, object], raw_permission)
    allowed_keys = {"external_directory_read", "external_directory_write", "rules"}
    unknown_keys = sorted(str(key) for key in payload if key not in allowed_keys)
    if unknown_keys:
        raise ValueError(f"persisted runtime_config permission field '{unknown_keys[0]}' is not supported")
    missing_keys = sorted({"external_directory_read", "external_directory_write"} - payload.keys())
    if missing_keys and not allow_missing_scopes:
        raise ValueError("persisted runtime_config permission is missing required field(s): " + ", ".join(missing_keys))
    defaults = ExternalDirectoryPermissionConfig()
    return ExternalDirectoryPermissionConfig(
        read=ExternalDirectoryPolicy(
            rules=parse_persisted_external_permission_rules(
                payload.get("external_directory_read", dict(defaults.read.rules)),
                field_path="permission.external_directory_read",
            )
        ),
        write=ExternalDirectoryPolicy(
            rules=parse_persisted_external_permission_rules(
                payload.get("external_directory_write", dict(defaults.write.rules)),
                field_path="permission.external_directory_write",
            )
        ),
        rules=parse_persisted_pattern_permission_rules(payload.get("rules")),
    )


def parse_persisted_runtime_tools_config(raw_value: object) -> RuntimeToolsConfig | None:
    try:
        return parse_runtime_tools_payload(raw_value, source="persisted runtime_config.tools")
    except ValueError as exc:
        raise ValueError(format_invalid_provider_config_error("persisted runtime_config.tools", str(exc))) from exc


def parse_persisted_external_permission_rules(
    raw_rules: object,
    *,
    field_path: str,
) -> tuple[tuple[str, PermissionDecision], ...]:
    if not isinstance(raw_rules, dict):
        raise ValueError(f"persisted runtime_config {field_path} must be an object")
    parsed: list[tuple[str, PermissionDecision]] = []
    for raw_pattern, raw_decision in cast(dict[object, object], raw_rules).items():
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ValueError(f"persisted runtime_config {field_path} keys must be strings")
        decision = permission_decision_or_none(raw_decision)
        if decision is None:
            raise ValueError(f"persisted runtime_config {field_path}.{raw_pattern} must be allow, deny, or ask")
        parsed.append((raw_pattern, decision))
    return tuple(parsed)


def parse_persisted_pattern_permission_rules(
    raw_rules: object,
) -> tuple[PatternPermissionRule, ...]:
    if raw_rules is None:
        return ()
    if not isinstance(raw_rules, list):
        raise ValueError("persisted runtime_config permission.rules must be an array")
    parsed: list[PatternPermissionRule] = []
    allowed_keys = {"tool", "path", "command", "decision"}
    for index, raw_rule in enumerate(raw_rules):
        field_path = f"permission.rules[{index}]"
        if not isinstance(raw_rule, dict):
            raise ValueError(f"persisted runtime_config {field_path} must be an object")
        payload = cast(dict[object, object], raw_rule)
        unknown_keys = sorted(str(key) for key in payload if key not in allowed_keys)
        if unknown_keys:
            raise ValueError(f"persisted runtime_config {field_path}.{unknown_keys[0]} is not supported")
        if "tool" not in payload:
            raise ValueError(f"persisted runtime_config {field_path}.tool is required")
        raw_tool = payload["tool"]
        if not isinstance(raw_tool, str) or not raw_tool.strip():
            raise ValueError(f"persisted runtime_config {field_path}.tool must be a string")
        raw_path = payload.get("path")
        if raw_path is not None and (not isinstance(raw_path, str) or not raw_path.strip()):
            raise ValueError(f"persisted runtime_config {field_path}.path must be a string")
        raw_command = payload.get("command")
        if raw_command is not None and (not isinstance(raw_command, str) or not raw_command.strip()):
            raise ValueError(f"persisted runtime_config {field_path}.command must be a string")
        decision = permission_decision_or_none(payload.get("decision"))
        if decision is None:
            raise ValueError(f"persisted runtime_config {field_path}.decision must be allow, deny, or ask")
        parsed.append(
            PatternPermissionRule(
                tool=raw_tool,
                path=raw_path,
                command=raw_command,
                decision=decision,
            )
        )
    return tuple(parsed)


def runtime_provider_config_metadata(
    serialized_providers: dict[str, object] | None,
) -> dict[str, object] | None:
    if not serialized_providers:
        return None
    retained: dict[str, object] = {}
    for provider_name, raw_provider in serialized_providers.items():
        if provider_name == "custom":
            if not isinstance(raw_provider, dict):
                continue
            retained_custom: dict[str, object] = {}
            for custom_name, raw_custom_provider in cast(dict[str, object], raw_provider).items():
                if not isinstance(raw_custom_provider, dict):
                    continue
                custom_provider_payload = cast(dict[str, object], raw_custom_provider)
                if "transient_retry" in custom_provider_payload:
                    retained_custom[custom_name] = {"transient_retry": custom_provider_payload["transient_retry"]}
            if retained_custom:
                retained[provider_name] = retained_custom
            continue
        if not isinstance(raw_provider, dict):
            continue
        provider_payload = cast(dict[str, object], raw_provider)
        if "transient_retry" in provider_payload:
            retained[provider_name] = {"transient_retry": provider_payload["transient_retry"]}
    return retained or None


def execution_engine_or_none(value: object) -> ExecutionEngineName | None:
    if value == "deterministic":
        return "deterministic"
    if value == "provider":
        return "provider"
    return None
