from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from ..agent import (
    AgentManifest,
    AgentManifestRegistry,
    AgentMcpBindingIntent,
    get_builtin_agent_manifest,
    is_valid_agent_manifest_id,
    list_builtin_agent_manifests,
    load_agent_manifest_registry,
)
from ..agent.prompts import has_builtin_prompt_profile, render_builtin_prompt_profile
from ..formatter import RuntimeFormatterPresetConfig
from ..hook.config import RuntimeHooksConfig
from ..hook.presets import validate_hook_preset_refs
from ..lsp import LspServerConfigOverride as RuntimeLspServerConfig
from ..lsp import derive_workspace_lsp_defaults, has_builtin_lsp_server_preset
from ..mcp.builtin import list_builtin_mcp_descriptors
from ..provider import config as provider_config
from ..provider.naming import (
    BUILTIN_PROVIDER_IDS,
    UnknownProviderIdError,
    canonical_provider_id,
)
from .config_models import (
    AGENT_PRESET_ID_PATTERN,
    AGENT_RUNTIME_INTERNAL_CONFIG_KEY,
    DEFAULT_HOOK_TIMEOUT_SECONDS,
    ENV_SETTINGS_LOCK,
    HOOK_COMMAND_FIELDS,
    TOP_LEVEL_ENV_VARS,
    AgentMcpBindingPayload,
    AgentPayload,
    AgentRuntimeInternalPayload,
    AgentToolsPayload,
    BackgroundTaskPayload,
    ContextWindowPayload,
    EnvironmentRuntimeSettings,
    ExecutionEngineName,
    FormatterPayload,
    FormatterPresetPayload,
    HooksPayload,
    LspPayload,
    LspServerPayload,
    McpPayload,
    McpTransport,
    PermissionPayload,
    PersistedAgentPayload,
    RuntimeAgentPromptSource,
    RuntimeConfigPayload,
    RuntimeContextTransformFailureMode,
    RuntimeMcpServerScope,
    RuntimeProviderContextDiagnosticMode,
    RuntimeTuiThemeMode,
    SkillsPayload,
    ToolsPayload,
    TuiPayload,
    UserConfigPayload,
    format_environment_validation_error,
    parse_approval_mode,
    parse_reasoning_effort,
    parse_tool_timeout_seconds,
    validate_config_model,
    validate_config_section,
)
from .config_models import (
    APPROVAL_MODE_ENV_VAR as APPROVAL_MODE_ENV_VAR,
)
from .config_models import (
    EXECUTION_ENGINE_ENV_VAR as EXECUTION_ENGINE_ENV_VAR,
)
from .config_models import (
    MODEL_ENV_VAR as MODEL_ENV_VAR,
)
from .config_models import (
    REASONING_EFFORT_ENV_VAR as REASONING_EFFORT_ENV_VAR,
)
from .config_models import (
    TOOL_TIMEOUT_ENV_VAR as TOOL_TIMEOUT_ENV_VAR,
)
from .context.transforms import validate_runtime_context_transform_refs
from .permission import (
    ExternalDirectoryPermissionConfig,
    ExternalDirectoryPolicy,
    PatternPermissionRule,
    PermissionDecision,
)
from .policy import RuntimePolicyConfig, validate_runtime_policy_config_payload

RuntimeProviderFallbackConfig = provider_config.ProviderFallbackConfig
RuntimeProvidersConfig = provider_config.ProviderConfigs
parse_provider_fallback_payload = provider_config.parse_provider_fallback_payload
parse_provider_configs_payload = provider_config.parse_provider_configs_payload
provider_configs_from_env = provider_config.provider_configs_from_env
merge_provider_configs = provider_config.merge_provider_configs
serialize_provider_fallback_config = provider_config.serialize_provider_fallback_config
serialize_provider_configs = provider_config.serialize_provider_configs

RUNTIME_CONFIG_FILE_NAME = ".voidcode.json"


def _running_on_windows() -> bool:
    return sys.platform == "win32"


DEFAULT_EXECUTION_ENGINE: ExecutionEngineName = "provider"


@dataclass(frozen=True, slots=True)
class RuntimeToolsBuiltinConfig:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeToolsLocalConfig:
    enabled: bool | None = None
    path: str = ".voidcode/tools"


@dataclass(frozen=True, slots=True)
class RuntimeToolsConfig:
    builtin: RuntimeToolsBuiltinConfig | None = None
    local: RuntimeToolsLocalConfig | None = None
    allowlist: tuple[str, ...] | None = None
    default: tuple[str, ...] | None = None
    #: Essential/discoverable tool split: when true, only the essential tool
    #: set (plus allowlist-required tools) is sent top-level to the provider;
    #: the rest stay registered and are reachable on demand via
    #: ``voidcode://tool/<name>`` doc reads and ``invoke_tool`` dispatch.
    #: Default false keeps the historical "all tools top-level" behavior.
    essential_only: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeFormatterConfig:
    enabled: bool | None = None
    #: Opt-in format-on-write switch (default off). ``enabled`` remains the
    #: existing alias; both map onto ``RuntimeHooksConfig.format_on_write``.
    format_on_write: bool | None = None
    languages: Mapping[str, RuntimeFormatterPresetConfig] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeSkillsConfig:
    enabled: bool | None = None
    paths: tuple[str, ...] = ()


def _empty_context_window_tool_limits() -> dict[str, int]:
    return {}


@dataclass(frozen=True, slots=True)
class RuntimeContextWindowConfig:
    default_tool_result_chars: int | None = 6_000
    per_tool_result_chars: Mapping[str, int] = field(default_factory=_empty_context_window_tool_limits)
    provider_context_diagnostics: RuntimeProviderContextDiagnosticMode = "warn"
    provider_context_oversized_feedback_chars: int = 8_000
    context_transform_failure_policy: RuntimeContextTransformFailureMode = "warn"
    summary_strategy: Literal["deterministic", "model_assisted"] = "deterministic"


@dataclass(frozen=True, slots=True)
class RuntimeLspConfig:
    enabled: bool | None = None
    servers: Mapping[str, RuntimeLspServerConfig] | None = None
    #: Opt-in automatic LSP diagnostics after edit/write (default off).
    #: Does not gate the explicit ``lsp`` tool.
    diagnostics_on_write: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeAcpConfig:
    enabled: bool | None = None
    handshake_request_type: str = "handshake"
    handshake_payload: dict[str, object] = field(default_factory=dict)


def _empty_background_task_concurrency_map() -> dict[str, int]:
    return {}


@dataclass(frozen=True, slots=True)
class RuntimeBackgroundTaskConfig:
    default_concurrency: int = 5
    provider_concurrency: Mapping[str, int] = field(default_factory=_empty_background_task_concurrency_map)
    model_concurrency: Mapping[str, int] = field(default_factory=_empty_background_task_concurrency_map)
    delegated_reminders_enabled: bool = True
    delegated_reminder_cooldown_seconds: int = 300


@dataclass(frozen=True, slots=True)
class RuntimeMcpServerConfig:
    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    scope: RuntimeMcpServerScope = "runtime"
    url: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeMcpConfig:
    enabled: bool | None = None
    servers: Mapping[str, RuntimeMcpServerConfig] | None = None
    request_timeout_seconds: float | None = None


def runtime_capability_enabled(enabled: bool | None) -> bool:
    """Resolve a capability ``enabled`` tri-state to a boolean.

    LSP/MCP tooling is enabled by default: ``None`` (unset) means enabled, and
    only an explicit ``enabled: false`` disables the capability. This is the
    single authority for the runtime-wide default-on convention; managers,
    doctor checks, and config parsing all resolve through it.
    """
    return enabled is not False


def _default_runtime_mcp_servers() -> dict[str, RuntimeMcpServerConfig]:
    servers: dict[str, RuntimeMcpServerConfig] = {}
    for descriptor in list_builtin_mcp_descriptors():
        if descriptor.skill_scoped:
            continue
        servers[descriptor.name] = RuntimeMcpServerConfig(
            transport=cast(McpTransport, descriptor.transport),
            command=descriptor.command,
            scope=cast(RuntimeMcpServerScope, descriptor.scope),
            url=descriptor.url,
        )
    return servers


def _default_runtime_mcp_config() -> RuntimeMcpConfig:
    return RuntimeMcpConfig(enabled=True, servers=_default_runtime_mcp_servers())


@dataclass(frozen=True, slots=True)
class RuntimeTuiConfig:
    leader_key: str | None = None
    keymap: Mapping[str, str] | None = None
    preferences: RuntimeTuiPreferences | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTuiThemePreferences:
    name: str | None = None
    mode: RuntimeTuiThemeMode | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTuiReadingPreferences:
    wrap: bool | None = None
    sidebar_collapsed: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTuiPreferences:
    theme: RuntimeTuiThemePreferences | None = None
    reading: RuntimeTuiReadingPreferences | None = None


@dataclass(frozen=True, slots=True)
class EffectiveRuntimeTuiPreferences:
    theme: RuntimeTuiThemePreferences
    reading: RuntimeTuiReadingPreferences


@dataclass(frozen=True, slots=True)
class RuntimeAgentInternalState:
    """Runtime-owned agent provenance and prompt materialization."""

    prompt_ref: str | None = None
    prompt_source: RuntimeAgentPromptSource | None = None
    prompt_materialization: Mapping[str, object] | None = None
    manifest_source_scope: str | None = None
    manifest_source_path: str | None = None
    manifest_tool_allowlist: tuple[str, ...] = ()
    manifest_skill_refs: tuple[str, ...] = ()
    manifest_hook_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeAgentConfig:
    preset: str
    prompt_profile: str | None = None
    prompt: str | None = None
    prompt_append: str | None = None
    hook_refs: tuple[str, ...] = ()
    context_transform_refs: tuple[str, ...] = ()
    model: str | None = None
    execution_engine: ExecutionEngineName | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    mcp_binding: AgentMcpBindingIntent | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    runtime_internal: RuntimeAgentInternalState | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    approval_mode: PermissionDecision = "ask"
    permission: ExternalDirectoryPermissionConfig = field(default_factory=ExternalDirectoryPermissionConfig)
    policy: RuntimePolicyConfig | None = None
    model: str | None = None
    execution_engine: ExecutionEngineName = DEFAULT_EXECUTION_ENGINE
    tool_timeout_seconds: int | None = None
    reasoning_effort: str | None = None
    hooks: RuntimeHooksConfig | None = None
    formatter: RuntimeFormatterConfig | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    context_window: RuntimeContextWindowConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    background_task: RuntimeBackgroundTaskConfig = field(default_factory=RuntimeBackgroundTaskConfig)
    mcp: RuntimeMcpConfig | None = field(default_factory=_default_runtime_mcp_config)
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    providers: RuntimeProvidersConfig | None = None
    agent: RuntimeAgentConfig | None = None
    agents: Mapping[str, RuntimeAgentConfig] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfigOverrides:
    approval_mode: PermissionDecision | None = None
    permission: ExternalDirectoryPermissionConfig | None = None
    policy: RuntimePolicyConfig | None = None
    model: str | None = None
    execution_engine: ExecutionEngineName | None = None
    tool_timeout_seconds: int | None = None
    tool_timeout_seconds_configured: bool = False
    reasoning_effort: str | None = None
    hooks: RuntimeHooksConfig | None = None
    formatter: RuntimeFormatterConfig | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    context_window: RuntimeContextWindowConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    background_task: RuntimeBackgroundTaskConfig | None = None
    mcp: RuntimeMcpConfig | None = None
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None
    providers: RuntimeProvidersConfig | None = None
    agent: RuntimeAgentConfig | None = None
    agents: Mapping[str, RuntimeAgentConfig] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeWebSettings:
    provider: str | None = None
    provider_api_key: str | None = None
    provider_api_key_present: bool = False


_BUILTIN_TUI_THEME_DEFAULTS: dict[RuntimeTuiThemeMode, str] = {
    "auto": "textual-dark",
    "light": "textual-light",
    "dark": "textual-dark",
}
_BUILTIN_TEXTUAL_LIGHT_THEMES: frozenset[str] = frozenset({"textual-light", "solarized-light", "atom-one-light"})
_BUILTIN_TEXTUAL_DARK_THEMES: frozenset[str] = frozenset(
    {
        "textual-dark",
        "nord",
        "gruvbox",
        "textual-ansi",
        "dracula",
        "tokyo-night",
        "monokai",
        "atom-one-dark",
    }
)


def runtime_config_path(workspace: Path) -> Path:
    return workspace / RUNTIME_CONFIG_FILE_NAME


def user_runtime_config_path() -> Path:
    return _user_runtime_config_path_from_env(os.environ)


def load_global_tui_preferences(
    env: Mapping[str, str] | None = None,
) -> RuntimeTuiPreferences | None:
    environment: Mapping[str, str] = os.environ if env is None else env
    global_config = _load_user_config(environment)
    if global_config.tui is None:
        return None
    return global_config.tui.preferences


def load_global_web_settings(env: Mapping[str, str] | None = None) -> RuntimeWebSettings:
    environment: Mapping[str, str] = os.environ if env is None else env
    global_config = _load_user_config(environment)
    providers = merge_provider_configs(global_config.providers, provider_configs_from_env(environment))
    payload = _read_json_object(_user_runtime_config_path_from_env(environment))
    raw_web = payload.get("web")
    configured_provider: str | None = None
    if isinstance(raw_web, dict):
        raw_provider = cast(dict[str, object], raw_web).get("provider")
        if isinstance(raw_provider, str):
            # A value written before provider ids were canonicalised (`MiniMax`)
            # names the same provider as `minimax`; report the canonical id so the
            # settings surface and `/api/providers` agree.
            configured_provider = canonical_provider_id(raw_provider)
    provider = configured_provider or _first_configured_provider_name(providers)
    return RuntimeWebSettings(
        provider=provider,
        provider_api_key_present=_provider_api_key_present(providers, provider),
    )


def load_workspace_tui_preferences(workspace: Path, env: Mapping[str, str] | None = None) -> RuntimeTuiPreferences | None:
    environment: Mapping[str, str] = os.environ if env is None else env
    repo_local = _load_repo_local_config(workspace.resolve(), env=environment)
    if repo_local.tui is None:
        return None
    return repo_local.tui.preferences


def load_runtime_config(
    workspace: Path,
    *,
    approval_mode: PermissionDecision | None = None,
    model: str | None = None,
    execution_engine: ExecutionEngineName | None = None,
    tool_timeout_seconds: int | None = None,
    reasoning_effort: str | None = None,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    resolved_workspace = workspace.resolve()
    environment: Mapping[str, str] = os.environ if env is None else env
    agent_registry = load_agent_manifest_registry(resolved_workspace, env=environment)
    env_overrides = _load_environment_runtime_config(environment)
    global_config = _load_user_config(environment)
    repo_local = _load_repo_local_config(
        resolved_workspace,
        env=environment,
        agent_registry=agent_registry,
    )
    resolved_tui = _resolve_tui_config(global_config.tui, repo_local.tui)
    resolved_lsp = repo_local.lsp or _derive_workspace_lsp_config(resolved_workspace)
    resolved_mcp = repo_local.mcp or _default_runtime_mcp_config()
    resolved_agent = _resolve_agent_config(repo_local.agent, agent_registry=agent_registry)

    env_providers = provider_configs_from_env(environment)
    resolved_providers = merge_provider_configs(
        repo_local.providers,
        merge_provider_configs(env_providers, global_config.providers),
    )

    return RuntimeConfig(
        approval_mode=_resolve_approval_mode(
            explicit=approval_mode,
            repo_local=repo_local.approval_mode,
            environment=env_overrides.approval_mode,
        ),
        permission=repo_local.permission or ExternalDirectoryPermissionConfig(),
        policy=repo_local.policy,
        model=_resolve_model(
            explicit=model,
            repo_local=repo_local.model,
            environment=env_overrides.model,
        ),
        execution_engine=_resolve_execution_engine(
            explicit=execution_engine,
            repo_local=repo_local.execution_engine,
            environment=env_overrides.execution_engine,
        ),
        tool_timeout_seconds=_resolve_tool_timeout_seconds(
            explicit=tool_timeout_seconds,
            repo_local=repo_local.tool_timeout_seconds,
            repo_local_configured=repo_local.tool_timeout_seconds_configured,
            environment=env_overrides.tool_timeout_seconds,
        ),
        reasoning_effort=_resolve_reasoning_effort(
            explicit=reasoning_effort,
            repo_local=repo_local.reasoning_effort,
            environment=env_overrides.reasoning_effort,
        ),
        hooks=_merge_hooks_configs(user=global_config.hooks, repo_local=repo_local.hooks),
        formatter=repo_local.formatter,
        tools=repo_local.tools,
        skills=repo_local.skills,
        context_window=repo_local.context_window,
        lsp=resolved_lsp,
        background_task=repo_local.background_task or RuntimeBackgroundTaskConfig(),
        mcp=resolved_mcp,
        tui=resolved_tui,
        provider_fallback=repo_local.provider_fallback,
        providers=resolved_providers,
        agent=resolved_agent,
        agents=repo_local.agents,
    )


def _derive_workspace_lsp_config(workspace: Path) -> RuntimeLspConfig | None:
    derived_servers = derive_workspace_lsp_defaults(workspace)
    if not derived_servers:
        return None
    return RuntimeLspConfig(enabled=True, servers=derived_servers)


def _load_repo_local_config(
    workspace: Path,
    *,
    env: Mapping[str, str],
    agent_registry: AgentManifestRegistry | None = None,
) -> RuntimeConfigOverrides:
    config_path = runtime_config_path(workspace)
    if not config_path.exists():
        return RuntimeConfigOverrides()

    payload = _read_json_object(config_path)

    # Sections whose validator lives in another module keep the first word: the
    # policy and provider parsers own their contract messages.
    policy = validate_runtime_policy_config_payload(
        payload.get("policy"),
        source="runtime config field 'policy'",
    )
    providers = _parse_providers_config(payload.get("providers"), env=env)
    provider_fallback = _parse_runtime_fallback_models_config(
        payload.get("fallback_models"),
        model=payload.get("model"),
    )

    config_payload = validate_config_model(
        RuntimeConfigPayload,
        payload,
        context={"config_file": str(config_path)},
    )

    hooks = _hooks_config_from_payload(config_payload.hooks)
    formatter = _formatter_config_from_payload(config_payload.formatter)
    hooks = _apply_formatter_config(hooks=hooks, formatter=formatter)

    return RuntimeConfigOverrides(
        approval_mode=config_payload.approval_mode,
        permission=_permission_config_from_payload(config_payload.permission),
        policy=policy,
        model=config_payload.model,
        execution_engine=config_payload.execution_engine,
        tool_timeout_seconds=config_payload.tool_timeout_seconds,
        tool_timeout_seconds_configured="tool_timeout_seconds" in config_payload.model_fields_set,
        reasoning_effort=config_payload.reasoning_effort,
        hooks=hooks,
        formatter=formatter,
        tools=_tools_config_from_payload(config_payload.tools),
        skills=_skills_config_from_payload(config_payload.skills),
        context_window=_context_window_config_from_payload(config_payload.context_window),
        lsp=_lsp_config_from_payload(config_payload.lsp),
        background_task=_background_task_config_from_payload(config_payload.background_task),
        mcp=_mcp_config_from_payload(config_payload.mcp),
        tui=_tui_config_from_payload(config_payload.tui),
        provider_fallback=provider_fallback,
        providers=providers,
        agent=_agent_config_from_payload(
            config_payload.agent,
            agent_registry=agent_registry,
        ),
        agents=_agents_config_from_payload(
            config_payload.agents,
            agent_registry=agent_registry,
        ),
    )


def _permission_config_from_payload(payload: PermissionPayload | None) -> ExternalDirectoryPermissionConfig | None:
    if payload is None:
        return None
    # Defaults stay here: an absent or empty rule map means "allow" for reads and
    # "ask" for writes, exactly as an absent ``permission`` block does.
    read_rules = tuple((payload.external_directory_read or {}).items()) or (("*", "allow"),)
    write_rules = tuple((payload.external_directory_write or {}).items()) or (("*", "ask"),)
    return ExternalDirectoryPermissionConfig(
        read=ExternalDirectoryPolicy(rules=read_rules),
        write=ExternalDirectoryPolicy(rules=write_rules),
        rules=tuple(
            PatternPermissionRule(tool=rule.tool, path=rule.path, command=rule.command, decision=rule.decision) for rule in payload.rules or ()
        ),
    )


def _load_user_config(env: Mapping[str, str]) -> RuntimeConfigOverrides:
    config_path = _user_runtime_config_path_from_env(env)
    if not config_path.exists():
        return RuntimeConfigOverrides()

    payload = _read_json_object(config_path)
    # The provider parser owns its messages, so it sees the raw payload.
    providers = _parse_providers_config(payload.get("providers"), env=env)
    config_payload = validate_config_model(UserConfigPayload, payload)
    return RuntimeConfigOverrides(
        tui=_tui_config_from_payload(config_payload.tui),
        providers=providers,
        hooks=_hooks_config_from_payload(config_payload.hooks),
    )


def _user_runtime_config_path_from_env(env: Mapping[str, str]) -> Path:
    if _running_on_windows():
        config_home = env.get("APPDATA") or os.environ.get("APPDATA")
        if config_home:
            return Path(config_home).expanduser() / "voidcode" / "config.json"
        local_config_home = env.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA")
        if local_config_home:
            return Path(local_config_home).expanduser() / "voidcode" / "config.json"
        return Path.home() / "AppData" / "Roaming" / "voidcode" / "config.json"

    config_home = env.get("XDG_CONFIG_HOME") or os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "voidcode" / "config.json"
    return Path.home() / ".config" / "voidcode" / "config.json"


def _hooks_config_from_payload(payload: HooksPayload | None) -> RuntimeHooksConfig | None:
    if payload is None:
        return None
    return RuntimeHooksConfig(
        enabled=payload.enabled,
        timeout_seconds=payload.timeout_seconds if payload.timeout_seconds is not None else DEFAULT_HOOK_TIMEOUT_SECONDS,
        failure_mode=payload.failure_mode,
        pre_tool=payload.pre_tool or (),
        pre_tool_match=payload.pre_tool_match or (),
        post_tool=payload.post_tool or (),
        post_tool_match=payload.post_tool_match or (),
        on_session_start=payload.on_session_start or (),
        on_session_end=payload.on_session_end or (),
        on_session_idle=payload.on_session_idle or (),
        on_background_task_registered=payload.on_background_task_registered or (),
        on_background_task_started=payload.on_background_task_started or (),
        on_background_task_progress=payload.on_background_task_progress or (),
        on_background_task_completed=payload.on_background_task_completed or (),
        on_background_task_failed=payload.on_background_task_failed or (),
        on_background_task_cancelled=payload.on_background_task_cancelled or (),
        on_background_task_interrupted=payload.on_background_task_interrupted or (),
        on_background_task_notification_enqueued=payload.on_background_task_notification_enqueued or (),
        on_background_task_result_read=payload.on_background_task_result_read or (),
        on_delegated_result_available=payload.on_delegated_result_available or (),
        on_turn_progress=payload.on_turn_progress or (),
        on_stuck_detected=payload.on_stuck_detected or (),
        on_approval_requested=payload.on_approval_requested or (),
        on_question_asked=payload.on_question_asked or (),
        on_before_compact=payload.on_before_compact or (),
        formatter_presets=_formatter_presets_from_payload(payload.formatter_presets, field_path="hooks.formatter_presets"),
    )


def _merge_hooks_configs(
    *,
    user: RuntimeHooksConfig | None,
    repo_local: RuntimeHooksConfig | None,
) -> RuntimeHooksConfig | None:
    """Concatenate user-global hook commands before repo-local ones, per surface.

    Scalar governance (``enabled``/``timeout_seconds``/``failure_mode``) stays
    repo-local: user config only contributes command tuples.
    """
    if user is None:
        return repo_local
    if repo_local is None:
        return user
    merged = {field_name: (*getattr(user, field_name), *getattr(repo_local, field_name)) for field_name in HOOK_COMMAND_FIELDS}
    # Match filters concatenate user-first like commands; empty still matches all.
    merged["pre_tool_match"] = (*user.pre_tool_match, *repo_local.pre_tool_match)
    merged["post_tool_match"] = (*user.post_tool_match, *repo_local.post_tool_match)
    return replace(repo_local, **merged)


def _formatter_config_from_payload(payload: FormatterPayload | None) -> RuntimeFormatterConfig | None:
    if payload is None:
        return None
    # An absent ``languages`` key means "no preset overrides" (the hooks-level
    # presets stay untouched); a present one merges over the built-in presets.
    languages = (
        _formatter_presets_from_payload(payload.languages, field_path="formatter.languages") if "languages" in payload.model_fields_set else {}
    )
    return RuntimeFormatterConfig(
        enabled=payload.enabled,
        format_on_write=payload.format_on_write,
        languages=languages,
    )


def _apply_formatter_config(
    *,
    hooks: RuntimeHooksConfig | None,
    formatter: RuntimeFormatterConfig | None,
) -> RuntimeHooksConfig | None:
    if formatter is None:
        return hooks
    base_hooks = hooks or RuntimeHooksConfig()
    formatter_presets = dict(base_hooks.formatter_presets)
    formatter_presets.update(formatter.languages)
    format_on_write = base_hooks.format_on_write
    if formatter.enabled is not None:
        format_on_write = formatter.enabled
    if formatter.format_on_write is not None:
        format_on_write = formatter.format_on_write
    return RuntimeHooksConfig(
        enabled=base_hooks.enabled,
        format_on_write=format_on_write,
        timeout_seconds=base_hooks.timeout_seconds,
        failure_mode=base_hooks.failure_mode,
        pre_tool=base_hooks.pre_tool,
        pre_tool_match=base_hooks.pre_tool_match,
        post_tool=base_hooks.post_tool,
        post_tool_match=base_hooks.post_tool_match,
        on_session_start=base_hooks.on_session_start,
        on_session_end=base_hooks.on_session_end,
        on_session_idle=base_hooks.on_session_idle,
        on_background_task_registered=base_hooks.on_background_task_registered,
        on_background_task_started=base_hooks.on_background_task_started,
        on_background_task_progress=base_hooks.on_background_task_progress,
        on_background_task_completed=base_hooks.on_background_task_completed,
        on_background_task_failed=base_hooks.on_background_task_failed,
        on_background_task_cancelled=base_hooks.on_background_task_cancelled,
        on_background_task_interrupted=base_hooks.on_background_task_interrupted,
        on_background_task_notification_enqueued=base_hooks.on_background_task_notification_enqueued,
        on_background_task_result_read=base_hooks.on_background_task_result_read,
        on_delegated_result_available=base_hooks.on_delegated_result_available,
        on_turn_progress=base_hooks.on_turn_progress,
        on_stuck_detected=base_hooks.on_stuck_detected,
        on_approval_requested=base_hooks.on_approval_requested,
        on_question_asked=base_hooks.on_question_asked,
        on_before_compact=base_hooks.on_before_compact,
        formatter_presets=formatter_presets,
    )


def _formatter_presets_from_payload(
    payload: Mapping[str, FormatterPresetPayload] | None,
    *,
    field_path: str,
) -> dict[str, RuntimeFormatterPresetConfig]:
    parsed_presets = dict(RuntimeHooksConfig().formatter_presets)
    if not payload:
        return parsed_presets
    for preset_name, preset in payload.items():
        builtin_preset = parsed_presets.get(preset_name)
        parsed_presets[preset_name] = _formatter_preset_from_payload(
            preset,
            field_path=f"{field_path}.{preset_name}",
            base_preset=builtin_preset,
        )
    return parsed_presets


def _formatter_preset_from_payload(
    preset: FormatterPresetPayload,
    *,
    field_path: str,
    base_preset: RuntimeFormatterPresetConfig | None,
) -> RuntimeFormatterPresetConfig:
    provided = preset.model_fields_set
    command = preset.command if "command" in provided else (base_preset.command if base_preset is not None else ())
    command = command or ()
    if not command:
        raise ValueError(f"runtime config field '{field_path}.command' must contain at least one string")
    extensions = preset.extensions if "extensions" in provided else (base_preset.extensions if base_preset is not None else ())
    extensions = extensions or ()
    root_markers = preset.root_markers if "root_markers" in provided else (base_preset.root_markers if base_preset is not None else ())
    root_markers = root_markers or ()
    fallback_commands = (
        preset.fallback_commands if "fallback_commands" in provided else (base_preset.fallback_commands if base_preset is not None else ())
    )
    fallback_commands = fallback_commands or ()
    cwd_policy = (
        preset.cwd_policy
        if "cwd_policy" in provided and preset.cwd_policy is not None
        else (base_preset.cwd_policy if base_preset is not None else "nearest_root")
    )
    if base_preset is None and not extensions:
        raise ValueError(f"runtime config field '{field_path}.extensions' must contain at least one string for custom formatter presets")
    return RuntimeFormatterPresetConfig(
        command=command,
        extensions=extensions,
        root_markers=root_markers,
        fallback_commands=fallback_commands,
        cwd_policy=cwd_policy,
    )


def _tools_config_from_payload(payload: ToolsPayload | AgentToolsPayload | None) -> RuntimeToolsConfig | None:
    if payload is None:
        return None
    local_config = payload.local if isinstance(payload, ToolsPayload) else None
    return RuntimeToolsConfig(
        builtin=None if payload.builtin is None else RuntimeToolsBuiltinConfig(enabled=payload.builtin.enabled),
        local=(None if local_config is None else RuntimeToolsLocalConfig(enabled=local_config.enabled, path=local_config.path or ".voidcode/tools")),
        allowlist=payload.allowlist,
        default=payload.default,
        essential_only=payload.essential_only is True,
    )


def _parse_tools_config(
    raw_tools: object,
    *,
    field_path: str = "tools",
    allow_local: bool = True,
) -> RuntimeToolsConfig | None:
    if raw_tools is None:
        return None
    model_type: type[ToolsPayload] | type[AgentToolsPayload] = ToolsPayload if allow_local else AgentToolsPayload
    return _tools_config_from_payload(
        validate_config_section(model_type, raw_tools, field_path=field_path),
    )


def _skills_config_from_payload(payload: SkillsPayload | None) -> RuntimeSkillsConfig | None:
    if payload is None:
        return None
    return RuntimeSkillsConfig(enabled=payload.enabled, paths=payload.paths or ())


def _context_window_config_from_payload(payload: ContextWindowPayload | None) -> RuntimeContextWindowConfig | None:
    if payload is None:
        return None
    return RuntimeContextWindowConfig(
        default_tool_result_chars=payload.default_tool_result_chars,
        per_tool_result_chars=dict(payload.per_tool_result_chars or {}),
        provider_context_diagnostics=payload.provider_context_diagnostics or "warn",
        provider_context_oversized_feedback_chars=payload.provider_context_oversized_feedback_chars or 8_000,
        context_transform_failure_policy=payload.context_transform_failure_policy or "warn",
        summary_strategy=payload.summary_strategy or "deterministic",
    )


def _parse_context_window_config(raw_context_window: object) -> RuntimeContextWindowConfig | None:
    if raw_context_window is None:
        return None
    return _context_window_config_from_payload(
        validate_config_section(ContextWindowPayload, raw_context_window, field_path="context_window"),
    )


def _lsp_config_from_payload(payload: LspPayload | None) -> RuntimeLspConfig | None:
    if payload is None:
        return None
    return RuntimeLspConfig(
        enabled=payload.enabled,
        servers=_lsp_servers_from_payload(payload.servers),
        diagnostics_on_write=payload.diagnostics_on_write is True,
    )


def _lsp_servers_from_payload(
    servers: Mapping[str, LspServerPayload] | None,
) -> dict[str, RuntimeLspServerConfig] | None:
    if servers is None:
        return None
    return {
        server_name: _lsp_server_from_payload(
            server_payload,
            server_name=server_name,
            field_path=f"lsp.servers.{server_name}",
        )
        for server_name, server_payload in servers.items()
    }


def _lsp_server_from_payload(
    payload: LspServerPayload,
    *,
    server_name: str,
    field_path: str,
) -> RuntimeLspServerConfig:
    preset = payload.preset
    uses_builtin_server_name = has_builtin_lsp_server_preset(server_name)
    if preset is not None and not has_builtin_lsp_server_preset(preset):
        raise ValueError(f"runtime config field '{field_path}.preset' references unknown preset")
    if not payload.command and preset is None and not uses_builtin_server_name:
        raise ValueError(f"runtime config field '{field_path}.command' must contain at least one string")
    return RuntimeLspServerConfig(
        preset=preset,
        command=payload.command or (),
        languages=payload.languages or (),
        extensions=payload.extensions or (),
        root_markers=payload.root_markers or (),
        settings=dict(payload.settings or {}),
        init_options=dict(payload.init_options or {}),
    )


def _mcp_config_from_payload(payload: McpPayload | None) -> RuntimeMcpConfig | None:
    if payload is None:
        return None
    parsed = RuntimeMcpConfig(
        enabled=payload.enabled,
        servers=(
            None
            if payload.servers is None
            else {
                server_name: RuntimeMcpServerConfig(
                    transport=server.transport or "stdio",
                    command=server.command or (),
                    env=dict(server.env or {}),
                    scope=server.scope or "runtime",
                    url=server.url,
                )
                for server_name, server in payload.servers.items()
            }
        ),
        request_timeout_seconds=payload.request_timeout_seconds,
    )
    # Unset ``enabled`` (None) means enabled by default; an absent ``servers``
    # block then behaves exactly like ``mcp.enabled: true`` today: load the
    # builtin remote MCP descriptors. Only explicit ``enabled: false`` keeps
    # the parsed section untouched.
    if runtime_capability_enabled(parsed.enabled) and parsed.servers is None:
        return RuntimeMcpConfig(
            enabled=True,
            servers=_default_runtime_mcp_servers(),
            request_timeout_seconds=parsed.request_timeout_seconds,
        )
    return parsed


def _tui_config_from_payload(payload: TuiPayload | None) -> RuntimeTuiConfig | None:
    if payload is None:
        return None
    return RuntimeTuiConfig(
        leader_key=payload.leader_key,
        keymap=(dict(payload.keymap) if payload.keymap is not None else None),
        preferences=(
            None
            if payload.preferences is None
            else RuntimeTuiPreferences(
                theme=(
                    None
                    if payload.preferences.theme is None
                    else RuntimeTuiThemePreferences(name=payload.preferences.theme.name, mode=payload.preferences.theme.mode)
                ),
                reading=(
                    None
                    if payload.preferences.reading is None
                    else RuntimeTuiReadingPreferences(
                        wrap=payload.preferences.reading.wrap,
                        sidebar_collapsed=payload.preferences.reading.sidebar_collapsed,
                    )
                ),
            )
        ),
    )


def _parse_tui_config(raw_tui: object) -> RuntimeTuiConfig | None:
    if raw_tui is None:
        return None
    return _tui_config_from_payload(
        validate_config_section(TuiPayload, raw_tui, field_path="tui"),
    )


def _background_task_config_from_payload(payload: BackgroundTaskPayload | None) -> RuntimeBackgroundTaskConfig | None:
    if payload is None:
        return None
    return RuntimeBackgroundTaskConfig(
        default_concurrency=payload.default_concurrency,
        provider_concurrency=dict(payload.provider_concurrency or {}),
        model_concurrency=dict(payload.model_concurrency or {}),
        delegated_reminders_enabled=payload.delegated_reminders_enabled is not False,
        delegated_reminder_cooldown_seconds=payload.delegated_reminder_cooldown_seconds,
    )


_AGENT_ID_RE = re.compile(AGENT_PRESET_ID_PATTERN)


def _agent_config_from_payload(
    payload: AgentPayload | PersistedAgentPayload | None,
    *,
    preset_override: str | None = None,
    agent_registry: AgentManifestRegistry | None = None,
) -> RuntimeAgentConfig | None:
    """Map a validated agent payload onto the runtime agent config.

    Preset validity, prompt materialization and fallback resolution stay here:
    they depend on the agent registry and on hooks, which the payload models do
    not own.
    """
    if payload is None:
        return None
    preset = payload.preset if payload.preset is not None else preset_override
    if preset is None:
        raise ValueError("runtime config field 'agent.preset' is required")
    if not isinstance(preset, str) or not is_valid_agent_manifest_id(preset):
        valid_presets = _valid_agent_preset_message(agent_registry)
        raise ValueError(f"runtime config field 'agent.preset' must be one of: {valid_presets}")
    manifest = _agent_manifest_for_preset(preset, agent_registry)

    internal_payload = payload.runtime_internal if isinstance(payload, PersistedAgentPayload) else None
    prompt_materialization = None if internal_payload is None else internal_payload.prompt_materialization
    has_persisted_custom_materialization = prompt_materialization is not None and prompt_materialization.get("source") == "custom_markdown"
    if manifest is None and not has_persisted_custom_materialization:
        valid_presets = _valid_agent_preset_message(agent_registry)
        raise ValueError(f"runtime config field 'agent.preset' must be one of: {valid_presets}")

    prompt_ref = internal_payload.prompt_ref if internal_payload is not None else None
    prompt_source = internal_payload.prompt_source if internal_payload is not None else None
    if prompt_source is not None and prompt_ref is None and prompt_materialization is None:
        raise ValueError("runtime config field 'agent.runtime_internal.prompt_ref' is required with prompt_source")
    if prompt_ref is not None and not has_builtin_prompt_profile(prompt_ref):
        raise ValueError("runtime config field 'agent.runtime_internal.prompt_ref' references unknown prompt profile")
    normalized_prompt_source = "builtin" if prompt_ref is not None else None
    if prompt_materialization is not None:
        raw_source = prompt_materialization.get("source")
        if prompt_source is not None and prompt_source != raw_source:
            raise ValueError("runtime config field 'agent.runtime_internal.prompt_source' must match prompt_materialization.source")
        if raw_source == "custom_markdown":
            normalized_prompt_source = "custom_markdown"
        elif raw_source == "builtin" and prompt_ref is not None:
            normalized_prompt_source = "builtin"

    return RuntimeAgentConfig(
        preset=preset,
        prompt_profile=payload.prompt_profile if payload.prompt_profile is not None else prompt_ref,
        prompt=payload.prompt,
        prompt_append=payload.prompt_append,
        runtime_internal=RuntimeAgentInternalState(
            prompt_ref=prompt_ref,
            prompt_source=cast(RuntimeAgentPromptSource, normalized_prompt_source),
            prompt_materialization=dict(prompt_materialization) if prompt_materialization is not None else None,
            manifest_source_scope=internal_payload.manifest_source_scope if internal_payload is not None else None,
            manifest_source_path=internal_payload.manifest_source_path if internal_payload is not None else None,
            manifest_tool_allowlist=internal_payload.manifest_tool_allowlist if internal_payload is not None else (),
            manifest_skill_refs=internal_payload.manifest_skill_refs if internal_payload is not None else (),
            manifest_hook_refs=_validate_agent_manifest_hook_refs(internal_payload),
        ),
        hook_refs=_validate_agent_hook_refs(payload.hook_refs or ()),
        context_transform_refs=_validate_agent_context_transform_refs(payload.context_transform_refs or ()),
        model=payload.model,
        execution_engine=(payload.execution_engine if isinstance(payload, PersistedAgentPayload) else None),
        tools=_tools_config_from_payload(payload.tools),
        skills=_skills_config_from_payload(payload.skills),
        mcp_binding=_agent_mcp_binding_from_payload(payload.mcp_binding),
        provider_fallback=_parse_agent_provider_fallback_config(payload.fallback_models, model=payload.model),
    )


def _validate_agent_manifest_hook_refs(internal_payload: AgentRuntimeInternalPayload | None) -> tuple[str, ...]:
    refs = internal_payload.manifest_hook_refs if internal_payload is not None else ()
    if not refs:
        return ()
    return validate_hook_preset_refs(
        refs,
        field_path="runtime config field 'agent.manifest_hook_refs'",
    )


def _validate_agent_hook_refs(hook_refs: tuple[str, ...]) -> tuple[str, ...]:
    if not hook_refs:
        return ()
    return validate_hook_preset_refs(hook_refs, field_path="runtime config field 'agent.hook_refs'")


def _validate_agent_context_transform_refs(refs: tuple[str, ...]) -> tuple[str, ...]:
    if not refs:
        return ()
    return validate_runtime_context_transform_refs(
        refs,
        field_path="runtime config field 'agent.context_transform_refs'",
    )


def _agent_mcp_binding_from_payload(payload: AgentMcpBindingPayload | None) -> AgentMcpBindingIntent | None:
    if payload is None:
        return None
    return AgentMcpBindingIntent(profile=payload.profile, servers=payload.servers or ())


def _parse_agent_config(
    raw_agent: object,
    *,
    agent_registry: AgentManifestRegistry | None = None,
    allow_runtime_internal: bool = False,
) -> RuntimeAgentConfig | None:
    if raw_agent is None:
        return None
    model_type: type[AgentPayload] | type[PersistedAgentPayload] = PersistedAgentPayload if allow_runtime_internal else AgentPayload
    return _agent_config_from_payload(
        validate_config_section(model_type, raw_agent, field_path="agent"),
        agent_registry=agent_registry,
    )


def _resolve_agent_config(
    agent: RuntimeAgentConfig | None,
    *,
    agent_registry: AgentManifestRegistry | None = None,
) -> RuntimeAgentConfig | None:
    if agent is None:
        return None
    manifest = _agent_manifest_for_preset(agent.preset, agent_registry)
    if manifest is not None:
        prompt_materialization = _resolve_agent_prompt_materialization(agent, manifest)
        provider_fallback = agent.provider_fallback
        model = agent.model or manifest.model_preference
        if provider_fallback is None and model is not None and manifest.fallback_models:
            provider_fallback = parse_provider_fallback_payload(
                {
                    "preferred_model": model,
                    "fallback_models": list(manifest.fallback_models),
                },
                source=f"agent manifest '{manifest.id}' fallback_models",
            )
        return RuntimeAgentConfig(
            preset=agent.preset,
            prompt_profile=agent.prompt_profile or manifest.prompt_profile,
            prompt=agent.prompt,
            prompt_append=agent.prompt_append,
            runtime_internal=RuntimeAgentInternalState(
                prompt_ref=agent.runtime_internal.prompt_ref if agent.runtime_internal is not None else None,
                prompt_source=(
                    agent.runtime_internal.prompt_source
                    if agent.runtime_internal is not None and agent.runtime_internal.prompt_source is not None
                    else (
                        manifest.prompt_materialization.source
                        if manifest.prompt_materialization is not None and manifest.prompt_materialization.source == "custom_markdown"
                        else None
                    )
                    or (
                        "custom_markdown"
                        if prompt_materialization is not None and prompt_materialization.get("source") == "custom_markdown"
                        else None
                    )
                ),
                prompt_materialization=prompt_materialization,
                manifest_source_scope=(
                    agent.runtime_internal.manifest_source_scope
                    if agent.runtime_internal is not None and agent.runtime_internal.manifest_source_scope is not None
                    else manifest.source_scope
                ),
                manifest_source_path=(
                    agent.runtime_internal.manifest_source_path
                    if agent.runtime_internal is not None and agent.runtime_internal.manifest_source_path is not None
                    else manifest.source_path
                ),
                manifest_tool_allowlist=(
                    agent.runtime_internal.manifest_tool_allowlist
                    if agent.runtime_internal is not None and agent.runtime_internal.manifest_tool_allowlist
                    else manifest.tool_allowlist
                ),
                manifest_skill_refs=(
                    agent.runtime_internal.manifest_skill_refs
                    if agent.runtime_internal is not None and agent.runtime_internal.manifest_skill_refs
                    else manifest.skill_refs
                ),
                manifest_hook_refs=(
                    agent.runtime_internal.manifest_hook_refs
                    if agent.runtime_internal is not None and agent.runtime_internal.manifest_hook_refs
                    else manifest.preset_hook_refs
                ),
            ),
            hook_refs=agent.hook_refs,
            context_transform_refs=agent.context_transform_refs,
            model=model,
            execution_engine=agent.execution_engine or manifest.execution_engine,
            tools=agent.tools,
            skills=agent.skills,
            mcp_binding=(agent.mcp_binding if agent.mcp_binding is not None else manifest.mcp_binding),
            provider_fallback=provider_fallback,
        )
    return agent


def _resolve_agent_prompt_materialization(
    agent: RuntimeAgentConfig,
    manifest: AgentManifest,
) -> Mapping[str, object] | None:
    internal = agent.runtime_internal
    if agent.prompt is None and agent.prompt_append is None:
        if internal is not None and internal.prompt_materialization is not None:
            return internal.prompt_materialization
        if manifest.prompt_materialization is not None and manifest.prompt_materialization.source == "custom_markdown":
            return manifest.prompt_materialization.to_payload(
                profile=agent.prompt_profile or manifest.prompt_materialization.profile,
            )
        return None

    base_prompt = agent.prompt
    if base_prompt is None:
        base_prompt = _base_prompt_for_manifest_override(agent, manifest)
    if base_prompt is None or not base_prompt.strip():
        raise ValueError(
            f"runtime config field 'agent.prompt_append' cannot be applied because agent preset '{agent.preset}' has no materialized base prompt"
        )
    return {
        "profile": agent.prompt_profile or manifest.prompt_profile or agent.preset,
        "version": 1,
        "source": "custom_markdown",
        "format": "markdown",
        "body": base_prompt.strip(),
        **({"prompt_append": agent.prompt_append} if agent.prompt_append is not None else {}),
        "source_scope": (
            internal.manifest_source_scope if internal is not None and internal.manifest_source_scope is not None else manifest.source_scope
        ),
        **(
            {
                "source_path": (
                    internal.manifest_source_path if internal is not None and internal.manifest_source_path is not None else manifest.source_path
                )
            }
            if (internal is not None and internal.manifest_source_path is not None) or manifest.source_path is not None
            else {}
        ),
    }


def _base_prompt_for_manifest_override(
    agent: RuntimeAgentConfig,
    manifest: AgentManifest,
) -> str | None:
    if agent.runtime_internal is not None and agent.runtime_internal.prompt_materialization is not None:
        body = agent.runtime_internal.prompt_materialization.get("body")
        if isinstance(body, str) and body.strip():
            return body.strip()
    materialization = manifest.prompt_materialization
    if materialization is not None:
        if materialization.body is not None and materialization.body.strip():
            return materialization.body.strip()
        selected_profile = materialization.select_profile(None)
        rendered = render_builtin_prompt_profile(selected_profile)
        if rendered is not None:
            return rendered
    prompt_profile = agent.prompt_profile or manifest.prompt_profile
    if prompt_profile is not None:
        return render_builtin_prompt_profile(prompt_profile)
    return None


def _agent_manifest_for_preset(
    preset: str,
    agent_registry: AgentManifestRegistry | None,
) -> AgentManifest | None:
    if agent_registry is not None:
        return agent_registry.get(preset)
    return get_builtin_agent_manifest(preset)


def _valid_agent_preset_message(agent_registry: AgentManifestRegistry | None) -> str:
    if agent_registry is None:
        return ", ".join(manifest.id for manifest in list_builtin_agent_manifests())
    return ", ".join(manifest.id for manifest in agent_registry.list_manifests())


def _parse_agent_provider_fallback_config(
    fallback_models: object,
    *,
    model: object,
) -> RuntimeProviderFallbackConfig | None:
    """Resolve an agent fallback chain through the provider boundary.

    The chain arrives unvalidated on purpose: the provider parser owns item types,
    duplicates and their messages (and reports them under
    ``agent.fallback_models.fallback_models[i]``, the path HEAD produced).
    """
    if fallback_models is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise ValueError("runtime config field 'agent.model' is required when 'agent.fallback_models' is provided")
    return parse_provider_fallback_payload(
        {
            "preferred_model": model.strip(),
            "fallback_models": fallback_models,
        },
        source="runtime config field 'agent.fallback_models'",
    )


def _agents_config_from_payload(
    payload: Mapping[str, AgentPayload] | None,
    *,
    agent_registry: AgentManifestRegistry | None = None,
) -> Mapping[str, RuntimeAgentConfig] | None:
    if payload is None:
        return None

    parsed: dict[str, RuntimeAgentConfig] = {}
    for key, entry in payload.items():
        if not _AGENT_ID_RE.fullmatch(key):
            raise ValueError("runtime config field 'agents' keys must match '^[a-z][a-z0-9_-]*$'")
        is_known_key = _agent_manifest_for_preset(key, agent_registry) is not None
        if entry.preset is None and not is_known_key:
            valid_presets = _valid_agent_preset_message(agent_registry)
            raise ValueError(f"runtime config field 'agents.{key}.preset' must be one of: {valid_presets}")

        try:
            parsed_entry = _agent_config_from_payload(
                entry,
                preset_override=key,
                agent_registry=agent_registry,
            )
        except ValueError as exc:
            # Agent diagnostics are written for the single ``agent`` block; an
            # entry of the ``agents`` map reports the same defect under its own key.
            message = str(exc).replace("agent.", f"agents.{key}.").replace("'agent'", f"'agents.{key}'")
            raise ValueError(message) from exc

        resolved_entry = _resolve_agent_config(
            parsed_entry,
            agent_registry=agent_registry,
        )
        if resolved_entry is None:
            raise ValueError(f"runtime config field 'agents.{key}' must resolve to a valid agent")
        parsed[key] = resolved_entry
    return parsed


def _parse_agents_config(
    raw_agents: object,
    *,
    agent_registry: AgentManifestRegistry | None = None,
    allow_runtime_internal: bool = False,
) -> Mapping[str, RuntimeAgentConfig] | None:
    if raw_agents is None:
        return None
    if not isinstance(raw_agents, dict):
        raise ValueError("runtime config field 'agents' must be an object when provided")
    parsed_entries: dict[str, AgentPayload] = {}
    for key, value in cast(dict[object, object], raw_agents).items():
        if not isinstance(key, str):
            raise ValueError("runtime config field 'agents' keys must be strings")
        if not isinstance(value, dict):
            raise ValueError(f"runtime config field 'agents.{key}' must be an object when provided")
        entry_type: type[AgentPayload] | type[PersistedAgentPayload] = PersistedAgentPayload if allow_runtime_internal else AgentPayload
        parsed_entries[key] = validate_config_section(entry_type, value, field_path=f"agents.{key}")
    return _agents_config_from_payload(parsed_entries, agent_registry=agent_registry)


def serialize_runtime_agents_config(
    agents: Mapping[str, RuntimeAgentConfig] | None,
    *,
    include_runtime_internal: bool = False,
) -> dict[str, object] | None:
    if agents is None:
        return None
    serialized: dict[str, object] = {}
    for agent_id, agent in agents.items():
        entry = serialize_runtime_agent_config(agent, include_runtime_internal=include_runtime_internal)
        if entry is not None:
            serialized[agent_id] = entry
    return serialized


def parse_runtime_agent_payload(
    raw_agent: object,
    *,
    source: str,
    hooks: RuntimeHooksConfig | None = None,
    agent_registry: AgentManifestRegistry | None = None,
    allow_runtime_internal: bool = False,
) -> RuntimeAgentConfig | None:
    # ``hooks`` is part of this boundary's signature and stays unused: hook preset
    # refs are validated against the hook registry, not against hook config.
    _ = hooks
    try:
        return _resolve_agent_config(
            _parse_agent_config(
                raw_agent,
                agent_registry=agent_registry,
                allow_runtime_internal=allow_runtime_internal,
            ),
            agent_registry=agent_registry,
        )
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def serialize_runtime_agent_config(
    agent: RuntimeAgentConfig | None,
    *,
    include_runtime_internal: bool = False,
) -> dict[str, object] | None:
    if agent is None:
        return None
    payload: dict[str, object] = {"preset": agent.preset}
    if agent.prompt_profile is not None:
        payload["prompt_profile"] = agent.prompt_profile
    if agent.prompt is not None:
        payload["prompt"] = agent.prompt
    if agent.prompt_append is not None:
        payload["prompt_append"] = agent.prompt_append
    if agent.hook_refs:
        payload["hook_refs"] = list(agent.hook_refs)
    if agent.context_transform_refs:
        payload["context_transform_refs"] = list(agent.context_transform_refs)
    if agent.model is not None:
        payload["model"] = agent.model
    if agent.tools is not None:
        payload["tools"] = _serialize_runtime_agent_tools_config(agent.tools)
    if agent.skills is not None:
        payload["skills"] = {"enabled": agent.skills.enabled, "paths": list(agent.skills.paths) if agent.skills.paths else None}
    if agent.mcp_binding is not None:
        payload["mcp_binding"] = agent.mcp_binding.to_payload()
    if agent.provider_fallback is not None:
        payload["fallback_models"] = list(agent.provider_fallback.fallback_models)
    if include_runtime_internal:
        internal = agent.runtime_internal
        manifest = get_builtin_agent_manifest(agent.preset)
        internal_payload: dict[str, object] = {}
        if internal is not None and internal.prompt_materialization is not None:
            internal_payload["prompt_materialization"] = dict(internal.prompt_materialization)
        elif manifest is not None and manifest.prompt_materialization is not None:
            internal_payload["prompt_materialization"] = manifest.prompt_materialization.to_payload(
                profile=agent.prompt_profile or manifest.prompt_materialization.profile,
            )
        if internal is not None:
            if internal.prompt_ref is not None:
                internal_payload["prompt_ref"] = internal.prompt_ref
            if internal.prompt_source is not None and (internal.prompt_source != "builtin" or internal.prompt_ref is not None):
                internal_payload["prompt_source"] = internal.prompt_source
            if internal.manifest_source_scope not in (None, "builtin"):
                internal_payload["manifest_source_scope"] = internal.manifest_source_scope
                if internal.manifest_source_path is not None:
                    internal_payload["manifest_source_path"] = internal.manifest_source_path
                if internal.manifest_tool_allowlist:
                    internal_payload["manifest_tool_allowlist"] = list(internal.manifest_tool_allowlist)
                if internal.manifest_skill_refs:
                    internal_payload["manifest_skill_refs"] = list(internal.manifest_skill_refs)
                if internal.manifest_hook_refs:
                    internal_payload["manifest_hook_refs"] = list(internal.manifest_hook_refs)
        if internal_payload:
            payload[AGENT_RUNTIME_INTERNAL_CONFIG_KEY] = internal_payload
    return {key: value for key, value in payload.items() if value is not None}


def parse_runtime_agents_payload(
    raw_agents: object,
    *,
    source: str,
    hooks: RuntimeHooksConfig | None = None,
    agent_registry: AgentManifestRegistry | None = None,
    allow_runtime_internal: bool = False,
) -> Mapping[str, RuntimeAgentConfig] | None:
    # ``hooks`` is part of this boundary's signature and stays unused (see above).
    _ = hooks
    try:
        return _parse_agents_config(
            raw_agents,
            agent_registry=agent_registry,
            allow_runtime_internal=allow_runtime_internal,
        )
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def _parse_runtime_fallback_models_config(
    raw_fallback_models: object,
    *,
    model: object,
) -> RuntimeProviderFallbackConfig | None:
    if raw_fallback_models is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise ValueError("runtime config field 'model' is required when 'fallback_models' is provided")
    return parse_provider_fallback_payload(
        {
            "preferred_model": model.strip(),
            "fallback_models": raw_fallback_models,
        },
        source="runtime config field 'fallback_models'",
    )


def parse_runtime_context_window_payload(
    raw_context_window: object,
    *,
    source: str,
) -> RuntimeContextWindowConfig | None:
    try:
        return _parse_context_window_config(raw_context_window)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def parse_runtime_policy_payload(raw_policy: object, *, source: str) -> RuntimePolicyConfig | None:
    try:
        return validate_runtime_policy_config_payload(raw_policy, source=source)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def parse_runtime_tools_payload(raw_tools: object, *, source: str) -> RuntimeToolsConfig | None:
    try:
        return _parse_tools_config(raw_tools)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from exc


def serialize_runtime_context_window_config(
    context_window: RuntimeContextWindowConfig | None,
) -> dict[str, object] | None:
    if context_window is None:
        return None
    payload: dict[str, object] = {
        "version": 2,
        "provider_context_diagnostics": context_window.provider_context_diagnostics,
        "provider_context_oversized_feedback_chars": context_window.provider_context_oversized_feedback_chars,
        "context_transform_failure_policy": context_window.context_transform_failure_policy,
        "summary_strategy": context_window.summary_strategy,
    }
    if context_window.default_tool_result_chars is not None:
        payload["default_tool_result_chars"] = context_window.default_tool_result_chars
    if context_window.per_tool_result_chars:
        payload["per_tool_result_chars"] = dict(context_window.per_tool_result_chars)
    return payload


def serialize_runtime_background_task_config(
    background_task: RuntimeBackgroundTaskConfig,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "default_concurrency": background_task.default_concurrency,
        "delegated_reminders_enabled": background_task.delegated_reminders_enabled,
        "delegated_reminder_cooldown_seconds": (background_task.delegated_reminder_cooldown_seconds),
    }
    if background_task.provider_concurrency:
        payload["provider_concurrency"] = dict(background_task.provider_concurrency)
    if background_task.model_concurrency:
        payload["model_concurrency"] = dict(background_task.model_concurrency)
    return payload


def serialize_runtime_tools_config(config: RuntimeToolsConfig | None) -> dict[str, object] | None:
    if config is None:
        return None
    payload: dict[str, object | None] = {
        "builtin": None if config.builtin is None else {"enabled": config.builtin.enabled},
        "local": None if config.local is None else {"enabled": config.local.enabled, "path": config.local.path},
        "allowlist": list(config.allowlist) if config.allowlist is not None else None,
        "default": list(config.default) if config.default is not None else None,
    }
    if config.essential_only:
        payload["essential_only"] = True
    return {key: value for key, value in payload.items() if value is not None}


def _serialize_runtime_agent_tools_config(
    config: RuntimeToolsConfig | None,
) -> dict[str, object] | None:
    payload = serialize_runtime_tools_config(config)
    if payload is None:
        return None
    payload.pop("local", None)
    return payload


def _resolve_tui_config(global_tui: RuntimeTuiConfig | None, workspace_tui: RuntimeTuiConfig | None) -> RuntimeTuiConfig:
    leader_key = (
        (workspace_tui.leader_key if workspace_tui is not None else None) or (global_tui.leader_key if global_tui is not None else None) or "alt+x"
    )
    keymap = (
        workspace_tui.keymap
        if workspace_tui is not None and workspace_tui.keymap is not None
        else (global_tui.keymap if global_tui is not None else None)
    )
    theme, reading = _merged_tui_theme_and_reading(
        global_tui.preferences if global_tui is not None else None,
        workspace_tui.preferences if workspace_tui is not None else None,
    )
    return RuntimeTuiConfig(
        leader_key=leader_key,
        keymap=keymap,
        preferences=RuntimeTuiPreferences(theme=theme, reading=reading),
    )


def _merged_tui_theme_and_reading(
    global_preferences: RuntimeTuiPreferences | None,
    workspace_preferences: RuntimeTuiPreferences | None,
) -> tuple[RuntimeTuiThemePreferences, RuntimeTuiReadingPreferences]:
    """Merge the two preference surfaces; both halves are always populated.

    The wire shape declares ``theme``/``reading`` optional, so returning the two
    concrete halves keeps callers from having to re-assert that this merge
    always fills them.
    """
    global_theme = global_preferences.theme if global_preferences is not None else None
    workspace_theme = workspace_preferences.theme if workspace_preferences is not None else None
    global_reading = global_preferences.reading if global_preferences is not None else None
    workspace_reading = workspace_preferences.reading if workspace_preferences is not None else None
    theme = RuntimeTuiThemePreferences(
        name=(workspace_theme.name if workspace_theme is not None else None)
        or (global_theme.name if global_theme is not None else None)
        or _BUILTIN_TUI_THEME_DEFAULTS["auto"],
        mode=(workspace_theme.mode if workspace_theme is not None else None) or (global_theme.mode if global_theme is not None else None) or "auto",
    )
    reading = RuntimeTuiReadingPreferences(
        wrap=(
            workspace_reading.wrap
            if workspace_reading is not None and workspace_reading.wrap is not None
            else (global_reading.wrap if global_reading is not None and global_reading.wrap is not None else True)
        ),
        sidebar_collapsed=(
            workspace_reading.sidebar_collapsed
            if workspace_reading is not None and workspace_reading.sidebar_collapsed is not None
            else (global_reading.sidebar_collapsed if global_reading is not None and global_reading.sidebar_collapsed is not None else False)
        ),
    )
    return theme, reading


def merge_runtime_tui_preferences(
    base_preferences: RuntimeTuiPreferences | None,
    override_preferences: RuntimeTuiPreferences | None,
) -> RuntimeTuiPreferences:
    theme, reading = _merged_tui_theme_and_reading(base_preferences, override_preferences)
    return RuntimeTuiPreferences(theme=theme, reading=reading)


def effective_runtime_tui_preferences(
    preferences: RuntimeTuiPreferences | None,
) -> EffectiveRuntimeTuiPreferences:
    theme, reading = _merged_tui_theme_and_reading(None, preferences)
    return EffectiveRuntimeTuiPreferences(theme=_resolve_theme_preferences(theme), reading=reading)


def _resolve_theme_preferences(
    theme_preferences: RuntimeTuiThemePreferences,
) -> RuntimeTuiThemePreferences:
    mode = theme_preferences.mode or "auto"
    name = theme_preferences.name or _BUILTIN_TUI_THEME_DEFAULTS[mode]
    if mode == "light" and name not in _BUILTIN_TEXTUAL_LIGHT_THEMES:
        name = _BUILTIN_TUI_THEME_DEFAULTS[mode]
    elif mode == "dark" and name not in _BUILTIN_TEXTUAL_DARK_THEMES:
        name = _BUILTIN_TUI_THEME_DEFAULTS[mode]
    elif mode == "auto" and name not in (_BUILTIN_TEXTUAL_LIGHT_THEMES | _BUILTIN_TEXTUAL_DARK_THEMES):
        name = _BUILTIN_TUI_THEME_DEFAULTS[mode]
    return RuntimeTuiThemePreferences(name=name, mode=mode)


def save_workspace_tui_preferences(workspace: Path, preferences: RuntimeTuiPreferences) -> None:
    _save_tui_preferences(runtime_config_path(workspace.resolve()), preferences)


def save_global_tui_preferences(preferences: RuntimeTuiPreferences) -> None:
    _save_tui_preferences(user_runtime_config_path(), preferences)


def save_global_web_settings(settings: RuntimeWebSettings) -> None:
    provider = canonical_provider_id(settings.provider) if isinstance(settings.provider, str) else ""
    if settings.provider_api_key is not None and not provider:
        raise ValueError("provider is required when saving a provider API key")
    config_path = user_runtime_config_path()
    payload = _read_json_object(config_path)
    if provider:
        _validate_runtime_web_provider(provider)
        raw_web = payload.get("web")
        web_payload = dict(cast(dict[str, object], raw_web)) if isinstance(raw_web, dict) else {}
        web_payload["provider"] = provider
        payload["web"] = web_payload
        if settings.provider_api_key is not None:
            payload["providers"] = _set_provider_api_key_payload(
                raw_providers=payload.get("providers"),
                provider=provider,
                api_key=settings.provider_api_key,
            )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _save_tui_preferences(config_path: Path, preferences: RuntimeTuiPreferences) -> None:
    payload = _read_json_object(config_path)
    tui_payload = cast(dict[str, object], payload.get("tui") if isinstance(payload.get("tui"), dict) else {})
    updated_tui_payload = dict(tui_payload)
    updated_tui_payload["preferences"] = serialize_runtime_tui_preferences(preferences)
    payload["tui"] = updated_tui_payload
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _validate_runtime_web_provider(provider: str) -> None:
    """Accept the provider ids the runtime lists, by canonical id.

    The settings form offers ``/api/providers`` entries; an id that is not a
    built-in provider must fail here rather than be written as a
    ``providers.custom`` declaration the user never made.
    """
    if not provider or "/" in provider:
        raise ValueError("provider must be a non-empty provider id without '/'")
    if provider not in BUILTIN_PROVIDER_IDS:
        raise ValueError(UnknownProviderIdError(provider).message)


def _first_configured_provider_name(providers: RuntimeProvidersConfig | None) -> str | None:
    """The first built-in provider that has a config block, in config-table order.

    The ids and their order come from ``PROVIDER_CONFIG_FIELDS``, so a new vendor
    is selectable without touching this function.
    """
    if providers is None:
        return None
    for provider_name in provider_config.PROVIDER_CONFIG_FIELDS:
        if providers.entry(provider_name) is not None:
            return provider_name
    if providers.custom:
        return next(iter(providers.custom))
    return None


def _provider_api_key_present(providers: RuntimeProvidersConfig | None, provider: str | None) -> bool:
    """Whether one provider has a credential, told by its own config shape.

    The entry type decides where the credential lives: ``google`` and ``copilot``
    nest it under ``auth``, every other config shape carries ``api_key``. The
    provider set itself comes from the config table.
    """
    if providers is None or provider is None:
        return False
    entry = providers.entry(provider)
    if entry is None:
        custom_provider = providers.custom.get(canonical_provider_id(provider))
        return bool(custom_provider and custom_provider.api_key)
    if isinstance(entry, provider_config.GoogleProviderConfig):
        return bool(entry.auth and entry.auth.api_key)
    if isinstance(entry, provider_config.CopilotProviderConfig):
        return bool(entry.auth and entry.auth.token)
    return bool(entry.api_key)


def _set_provider_api_key_payload(*, raw_providers: object, provider: str, api_key: str) -> dict[str, object]:
    """Write one provider's API key in that provider's own config shape.

    The provider set is the config table (``PROVIDER_CONFIG_FIELDS``), and every
    id in it writes a top-level ``api_key``; only ``google`` and ``copilot`` nest
    it under ``auth``, which no table carries.
    """
    if provider not in provider_config.PROVIDER_CONFIG_FIELDS:
        raise ValueError(f"provider '{provider}' has no writable credentials field in {RUNTIME_CONFIG_FILE_NAME}")
    providers_payload = dict(cast(dict[str, object], raw_providers)) if isinstance(raw_providers, dict) else {}
    nested = providers_payload.get(provider)
    nested_payload = dict(cast(dict[str, object], nested)) if isinstance(nested, dict) else {}
    if provider == "google":
        auth = nested_payload.get("auth")
        auth_payload = dict(cast(dict[str, object], auth)) if isinstance(auth, dict) else {"method": "api_key"}
        raw_method = auth_payload.get("method")
        auth_payload["method"] = raw_method if isinstance(raw_method, str) and raw_method else "api_key"
        auth_payload["api_key"] = api_key
        nested_payload["auth"] = auth_payload
    elif provider == "copilot":
        auth = nested_payload.get("auth")
        auth_payload = dict(cast(dict[str, object], auth)) if isinstance(auth, dict) else {"method": "token"}
        raw_method = auth_payload.get("method")
        auth_payload["method"] = raw_method if isinstance(raw_method, str) and raw_method else "token"
        auth_payload["token"] = api_key
        nested_payload["auth"] = auth_payload
    else:
        nested_payload["api_key"] = api_key
    providers_payload[provider] = nested_payload
    return providers_payload


def _read_json_object(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime config file must contain valid JSON: {config_path}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError(f"runtime config file must contain a JSON object: {config_path}")
    return cast(dict[str, object], raw_payload)


def serialize_runtime_tui_preferences(preferences: RuntimeTuiPreferences) -> dict[str, object]:
    payload: dict[str, object] = {}
    if preferences.theme is not None:
        theme_payload: dict[str, object] = {}
        if preferences.theme.name is not None:
            theme_payload["name"] = preferences.theme.name
        if preferences.theme.mode is not None:
            theme_payload["mode"] = preferences.theme.mode
        if theme_payload:
            payload["theme"] = theme_payload
    if preferences.reading is not None:
        reading_payload: dict[str, object] = {}
        if preferences.reading.wrap is not None:
            reading_payload["wrap"] = preferences.reading.wrap
        if preferences.reading.sidebar_collapsed is not None:
            reading_payload["sidebar_collapsed"] = preferences.reading.sidebar_collapsed
        if reading_payload:
            payload["reading"] = reading_payload
    return payload


def _parse_providers_config(
    raw_providers: object,
    *,
    env: Mapping[str, str],
) -> RuntimeProvidersConfig | None:
    return parse_provider_configs_payload(
        raw_providers,
        source="runtime config field 'providers'",
        env=env,
    )


def _load_environment_runtime_config(env: Mapping[str, str] | None) -> RuntimeConfigOverrides:
    try:
        with _temporary_runtime_environment(env):
            settings = EnvironmentRuntimeSettings()
    except ValidationError as exc:
        raise ValueError(format_environment_validation_error(exc)) from exc

    return RuntimeConfigOverrides(
        approval_mode=settings.approval_mode,
        model=settings.model,
        execution_engine=settings.execution_engine,
        tool_timeout_seconds=settings.tool_timeout_seconds,
        reasoning_effort=settings.reasoning_effort,
    )


@contextmanager
def _temporary_runtime_environment(env: Mapping[str, str] | None):
    if env is None:
        yield
        return

    with ENV_SETTINGS_LOCK:
        previous_values = {name: os.environ.get(name) for name in TOP_LEVEL_ENV_VARS}
        try:
            for name in TOP_LEVEL_ENV_VARS:
                if name in env:
                    os.environ[name] = env[name]
                else:
                    os.environ.pop(name, None)
            yield
        finally:
            for name, previous_value in previous_values.items():
                if previous_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous_value


def _resolve_approval_mode(
    *,
    explicit: PermissionDecision | None,
    repo_local: PermissionDecision | None,
    environment: str | None,
) -> PermissionDecision:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    parsed_environment = parse_approval_mode(
        environment,
        source=f"environment variable {APPROVAL_MODE_ENV_VAR}",
        allow_none=True,
    )
    if parsed_environment is not None:
        return parsed_environment
    return "ask"


def _resolve_model(*, explicit: str | None, repo_local: str | None, environment: str | None) -> str | None:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    if environment is not None:
        if not environment:
            raise ValueError(f"environment variable {MODEL_ENV_VAR} must be a non-empty string")
        return environment
    return None


def _resolve_execution_engine(
    *,
    explicit: ExecutionEngineName | None,
    repo_local: ExecutionEngineName | None,
    environment: ExecutionEngineName | None,
) -> ExecutionEngineName:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    if environment is not None:
        return environment
    return DEFAULT_EXECUTION_ENGINE


def _resolve_tool_timeout_seconds(
    *,
    explicit: int | None,
    repo_local: int | None,
    repo_local_configured: bool,
    environment: int | None,
) -> int | None:
    if explicit is not None:
        return parse_tool_timeout_seconds(
            explicit,
            source="explicit runtime config override 'tool_timeout_seconds'",
            allow_none=True,
        )
    if repo_local_configured:
        return repo_local
    if environment is not None:
        return environment
    return None


def _resolve_reasoning_effort(
    *,
    explicit: str | None,
    repo_local: str | None,
    environment: str | None,
) -> str | None:
    if explicit is not None:
        return parse_reasoning_effort(
            explicit,
            allow_none=True,
        )
    if repo_local is not None:
        return repo_local
    if environment is not None:
        return environment
    return None
