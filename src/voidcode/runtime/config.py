from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from ..hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
from ..lsp import LspServerConfigOverride as RuntimeLspServerConfig
from ..lsp import has_builtin_lsp_server_preset
from ..provider import config as provider_config
from .permission import PermissionDecision

RuntimeProviderFallbackConfig = provider_config.ProviderFallbackConfig
parse_provider_fallback_payload = provider_config.parse_provider_fallback_payload
serialize_provider_fallback_config = provider_config.serialize_provider_fallback_config

RUNTIME_CONFIG_FILE_NAME = ".voidcode.json"
APPROVAL_MODE_ENV_VAR = "VOIDCODE_APPROVAL_MODE"
MODEL_ENV_VAR = "VOIDCODE_MODEL"
_VALID_APPROVAL_MODES = ("allow", "deny", "ask")
_VALID_TUI_COMMANDS = ("command_palette", "session_new", "session_resume")

type ExecutionEngineName = Literal["deterministic", "single_agent"]

_VALID_EXECUTION_ENGINES: tuple[ExecutionEngineName, ...] = ("deterministic", "single_agent")


@dataclass(frozen=True, slots=True)
class RuntimeToolsBuiltinConfig:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeToolsConfig:
    builtin: RuntimeToolsBuiltinConfig | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeSkillsConfig:
    enabled: bool | None = None
    paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeLspConfig:
    enabled: bool | None = None
    servers: Mapping[str, RuntimeLspServerConfig] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeAcpConfig:
    enabled: bool | None = None


type McpTransport = Literal["stdio"]


@dataclass(frozen=True, slots=True)
class RuntimeMcpServerConfig:
    transport: McpTransport = "stdio"
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeMcpConfig:
    enabled: bool | None = None
    servers: Mapping[str, RuntimeMcpServerConfig] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeTuiConfig:
    leader_key: str = "alt+x"
    keymap: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    approval_mode: PermissionDecision = "ask"
    model: str | None = None
    execution_engine: ExecutionEngineName = "deterministic"
    max_steps: int = 4
    hooks: RuntimeHooksConfig | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    mcp: RuntimeMcpConfig | None = None
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfigOverrides:
    approval_mode: PermissionDecision | None = None
    model: str | None = None
    execution_engine: ExecutionEngineName | None = None
    max_steps: int | None = None
    hooks: RuntimeHooksConfig | None = None
    tools: RuntimeToolsConfig | None = None
    skills: RuntimeSkillsConfig | None = None
    lsp: RuntimeLspConfig | None = None
    acp: RuntimeAcpConfig | None = None
    mcp: RuntimeMcpConfig | None = None
    tui: RuntimeTuiConfig | None = None
    provider_fallback: RuntimeProviderFallbackConfig | None = None


def runtime_config_path(workspace: Path) -> Path:
    return workspace / RUNTIME_CONFIG_FILE_NAME


def load_runtime_config(
    workspace: Path,
    *,
    approval_mode: PermissionDecision | None = None,
    model: str | None = None,
    env: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    resolved_workspace = workspace.resolve()
    repo_local = _load_repo_local_config(resolved_workspace)
    environment = os.environ if env is None else env

    return RuntimeConfig(
        approval_mode=_resolve_approval_mode(
            explicit=approval_mode,
            repo_local=repo_local.approval_mode,
            environment=environment.get(APPROVAL_MODE_ENV_VAR),
        ),
        model=_resolve_model(
            explicit=model,
            repo_local=repo_local.model,
            environment=environment.get(MODEL_ENV_VAR),
        ),
        execution_engine=_resolve_execution_engine(repo_local=repo_local.execution_engine),
        max_steps=_resolve_max_steps(repo_local=repo_local.max_steps),
        hooks=repo_local.hooks,
        tools=repo_local.tools,
        skills=repo_local.skills,
        lsp=repo_local.lsp,
        acp=repo_local.acp,
        mcp=repo_local.mcp,
        tui=repo_local.tui,
        provider_fallback=repo_local.provider_fallback,
    )


def _load_repo_local_config(workspace: Path) -> RuntimeConfigOverrides:
    config_path = runtime_config_path(workspace)
    if not config_path.exists():
        return RuntimeConfigOverrides()

    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime config file must contain valid JSON: {config_path}") from exc

    if not isinstance(raw_payload, dict):
        raise ValueError(f"runtime config file must contain a JSON object: {config_path}")

    payload = cast(dict[str, object], raw_payload)

    raw_model = payload.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        raise ValueError("runtime config field 'model' must be a string when provided")

    raw_execution_engine = payload.get("execution_engine")
    parsed_execution_engine = _parse_execution_engine(
        raw_execution_engine,
        source=f"runtime config field 'execution_engine' in {config_path}",
        allow_none=True,
    )

    parsed_max_steps = _parse_max_steps(
        payload.get("max_steps"),
        source=f"runtime config field 'max_steps' in {config_path}",
        allow_none=True,
    )

    raw_hooks = payload.get("hooks")
    hooks = _parse_hooks_config(raw_hooks)

    raw_tools = payload.get("tools")
    tools = _parse_tools_config(raw_tools)

    raw_skills = payload.get("skills")
    skills = _parse_skills_config(raw_skills)

    raw_lsp = payload.get("lsp")
    lsp = _parse_lsp_config(raw_lsp)

    raw_acp = payload.get("acp")
    acp = _parse_acp_config(raw_acp)

    raw_mcp = payload.get("mcp")
    mcp = _parse_mcp_config(raw_mcp)

    raw_tui = payload.get("tui")
    tui = _parse_tui_config(raw_tui)

    raw_provider_fallback = payload.get("provider_fallback")
    provider_fallback = _parse_provider_fallback_config(raw_provider_fallback)

    raw_approval_mode = payload.get("approval_mode")
    parsed_approval_mode = _parse_approval_mode(
        raw_approval_mode,
        source=f"runtime config field 'approval_mode' in {config_path}",
        allow_none=True,
    )

    return RuntimeConfigOverrides(
        approval_mode=parsed_approval_mode,
        model=raw_model,
        execution_engine=parsed_execution_engine,
        max_steps=parsed_max_steps,
        hooks=hooks,
        tools=tools,
        skills=skills,
        lsp=lsp,
        acp=acp,
        mcp=mcp,
        tui=tui,
        provider_fallback=provider_fallback,
    )


def _parse_hooks_config(raw_hooks: object) -> RuntimeHooksConfig | None:
    if raw_hooks is None:
        return None
    if not isinstance(raw_hooks, dict):
        raise ValueError("runtime config field 'hooks' must be an object when provided")

    hooks_payload = cast(dict[str, object], raw_hooks)
    enabled = hooks_payload.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("runtime config field 'hooks.enabled' must be a boolean when provided")

    pre_tool = _parse_command_list(hooks_payload.get("pre_tool"), field_path="hooks.pre_tool")
    post_tool = _parse_command_list(hooks_payload.get("post_tool"), field_path="hooks.post_tool")
    formatter_presets: dict[str, RuntimeFormatterPresetConfig] = _parse_formatter_presets_config(
        hooks_payload.get("formatter_presets"),
        field_path="hooks.formatter_presets",
    )

    return RuntimeHooksConfig(
        enabled=enabled,
        pre_tool=pre_tool,
        post_tool=post_tool,
        formatter_presets=formatter_presets,
    )


def _parse_formatter_presets_config(
    raw_value: object, *, field_path: str
) -> dict[str, RuntimeFormatterPresetConfig]:
    parsed_presets = dict(RuntimeHooksConfig().formatter_presets)
    if raw_value is None:
        return parsed_presets
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    raw_presets = cast(dict[str, object], raw_value)
    for preset_name, raw_preset in raw_presets.items():
        parsed_presets[preset_name] = _parse_formatter_preset_config(
            raw_preset,
            field_path=f"{field_path}.{preset_name}",
        )
    return parsed_presets


def _parse_formatter_preset_config(
    raw_value: object, *, field_path: str
) -> RuntimeFormatterPresetConfig:
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object")

    preset_payload = cast(dict[str, object], raw_value)
    command = _parse_string_list(preset_payload.get("command"), field_path=f"{field_path}.command")
    if not command:
        raise ValueError(
            f"runtime config field '{field_path}.command' must contain at least one string"
        )
    return RuntimeFormatterPresetConfig(command=command)


def _parse_tools_config(raw_tools: object) -> RuntimeToolsConfig | None:
    if raw_tools is None:
        return None
    if not isinstance(raw_tools, dict):
        raise ValueError("runtime config field 'tools' must be an object when provided")

    tools_payload = cast(dict[str, object], raw_tools)
    builtin = _parse_tools_builtin_config(tools_payload.get("builtin"))
    paths = _parse_string_list(tools_payload.get("paths"), field_path="tools.paths")
    return RuntimeToolsConfig(builtin=builtin, paths=paths)


def _parse_tools_builtin_config(raw_builtin: object) -> RuntimeToolsBuiltinConfig | None:
    if raw_builtin is None:
        return None
    if not isinstance(raw_builtin, dict):
        raise ValueError("runtime config field 'tools.builtin' must be an object when provided")

    builtin_payload = cast(dict[str, object], raw_builtin)
    enabled = _parse_optional_bool(
        builtin_payload.get("enabled"), field_path="tools.builtin.enabled"
    )
    return RuntimeToolsBuiltinConfig(enabled=enabled)


def _parse_skills_config(raw_skills: object) -> RuntimeSkillsConfig | None:
    if raw_skills is None:
        return None
    if not isinstance(raw_skills, dict):
        raise ValueError("runtime config field 'skills' must be an object when provided")

    skills_payload = cast(dict[str, object], raw_skills)
    enabled = _parse_optional_bool(skills_payload.get("enabled"), field_path="skills.enabled")
    paths = _parse_string_list(skills_payload.get("paths"), field_path="skills.paths")
    return RuntimeSkillsConfig(enabled=enabled, paths=paths)


def _parse_lsp_config(raw_lsp: object) -> RuntimeLspConfig | None:
    if raw_lsp is None:
        return None
    if not isinstance(raw_lsp, dict):
        raise ValueError("runtime config field 'lsp' must be an object when provided")

    lsp_payload = cast(dict[str, object], raw_lsp)
    enabled = _parse_optional_bool(lsp_payload.get("enabled"), field_path="lsp.enabled")
    servers = _parse_lsp_servers_config(lsp_payload.get("servers"), field_path="lsp.servers")
    return RuntimeLspConfig(enabled=enabled, servers=servers)


def _parse_lsp_servers_config(
    raw_value: object, *, field_path: str
) -> dict[str, RuntimeLspServerConfig] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    raw_servers = cast(dict[str, object], raw_value)
    parsed_servers: dict[str, RuntimeLspServerConfig] = {}
    for server_name, raw_server in raw_servers.items():
        parsed_servers[server_name] = _parse_lsp_server_config(
            raw_server,
            server_name=server_name,
            field_path=f"{field_path}.{server_name}",
        )
    return parsed_servers


def _parse_lsp_server_config(
    raw_value: object,
    *,
    server_name: str,
    field_path: str,
) -> RuntimeLspServerConfig:
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object")

    server_payload = cast(dict[str, object], raw_value)
    preset = server_payload.get("preset")
    if preset is not None:
        if not isinstance(preset, str) or not preset:
            raise ValueError(f"runtime config field '{field_path}.preset' must be a string")
        if not has_builtin_lsp_server_preset(preset):
            raise ValueError(
                f"runtime config field '{field_path}.preset' references unknown preset"
            )
    command = _parse_string_list(server_payload.get("command"), field_path=f"{field_path}.command")
    if not command and preset is None and not has_builtin_lsp_server_preset(server_name):
        raise ValueError(
            f"runtime config field '{field_path}.command' must contain at least one string"
        )
    languages = _parse_string_list(
        server_payload.get("languages"),
        field_path=f"{field_path}.languages",
    )
    extensions = _parse_string_list(
        server_payload.get("extensions"),
        field_path=f"{field_path}.extensions",
    )
    root_markers = _parse_string_list(
        server_payload.get("root_markers"),
        field_path=f"{field_path}.root_markers",
    )
    settings = _parse_object_container(
        server_payload.get("settings"),
        field_path=f"{field_path}.settings",
    )
    init_options = _parse_object_container(
        server_payload.get("init_options"),
        field_path=f"{field_path}.init_options",
    )
    return RuntimeLspServerConfig(
        preset=preset,
        command=command,
        languages=languages,
        extensions=extensions,
        root_markers=root_markers,
        settings=settings or {},
        init_options=init_options or {},
    )


def _parse_acp_config(raw_acp: object) -> RuntimeAcpConfig | None:
    if raw_acp is None:
        return None
    if not isinstance(raw_acp, dict):
        raise ValueError("runtime config field 'acp' must be an object when provided")

    acp_payload = cast(dict[str, object], raw_acp)
    enabled = _parse_optional_bool(acp_payload.get("enabled"), field_path="acp.enabled")
    return RuntimeAcpConfig(enabled=enabled)


def _parse_mcp_config(raw_mcp: object) -> RuntimeMcpConfig | None:
    if raw_mcp is None:
        return None
    if not isinstance(raw_mcp, dict):
        raise ValueError("runtime config field 'mcp' must be an object when provided")

    mcp_payload = cast(dict[str, object], raw_mcp)
    enabled = _parse_optional_bool(mcp_payload.get("enabled"), field_path="mcp.enabled")
    servers = _parse_mcp_servers_config(mcp_payload.get("servers"), field_path="mcp.servers")
    return RuntimeMcpConfig(enabled=enabled, servers=servers)


def _parse_mcp_servers_config(
    raw_value: object, *, field_path: str
) -> dict[str, RuntimeMcpServerConfig] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")

    raw_servers = cast(dict[str, object], raw_value)
    parsed_servers: dict[str, RuntimeMcpServerConfig] = {}
    for server_name, raw_server in raw_servers.items():
        parsed_servers[server_name] = _parse_mcp_server_config(
            raw_server,
            field_path=f"{field_path}.{server_name}",
        )
    return parsed_servers


def _parse_mcp_server_config(raw_value: object, *, field_path: str) -> RuntimeMcpServerConfig:
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object")

    server_payload = cast(dict[str, object], raw_value)
    transport = server_payload.get("transport", "stdio")
    if transport != "stdio":
        raise ValueError(f"runtime config field '{field_path}.transport' must be one of: stdio")
    command = _parse_string_list(server_payload.get("command"), field_path=f"{field_path}.command")
    if not command:
        raise ValueError(
            f"runtime config field '{field_path}.command' must contain at least one string"
        )

    raw_env = server_payload.get("env")
    env: dict[str, str] = {}
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            raise ValueError(f"runtime config field '{field_path}.env' must be an object")
        env_payload = cast(dict[str, object], raw_env)
        for key, value in env_payload.items():
            if not isinstance(value, str):
                raise ValueError(f"runtime config field '{field_path}.env.{key}' must be a string")
            env[key] = value

    return RuntimeMcpServerConfig(transport="stdio", command=command, env=env)


def _parse_tui_config(raw_tui: object) -> RuntimeTuiConfig | None:
    if raw_tui is None:
        return None
    if not isinstance(raw_tui, dict):
        raise ValueError("runtime config field 'tui' must be an object when provided")

    tui_payload = cast(dict[str, object], raw_tui)
    leader_key = tui_payload.get("leader_key")
    if leader_key is not None and not isinstance(leader_key, str):
        raise ValueError("runtime config field 'tui.leader_key' must be a string when provided")

    keymap: Mapping[str, str] | None = None
    raw_keymap = tui_payload.get("keymap")
    if raw_keymap is not None:
        if not isinstance(raw_keymap, dict):
            raise ValueError("runtime config field 'tui.keymap' must be an object when provided")
        dict_keymap = cast(dict[str, object], raw_keymap)
        for value in dict_keymap.values():
            if not isinstance(value, str):
                raise ValueError("runtime config field 'tui.keymap' values must be strings")
            if value not in _VALID_TUI_COMMANDS:
                allowed = ", ".join(_VALID_TUI_COMMANDS)
                raise ValueError(
                    f"runtime config field 'tui.keymap' values must be one of: {allowed}"
                )
        keymap = cast(dict[str, str], raw_keymap)

    return RuntimeTuiConfig(
        leader_key=leader_key if leader_key is not None else "alt+x",
        keymap=keymap,
    )


def _parse_provider_fallback_config(
    raw_provider_fallback: object,
) -> RuntimeProviderFallbackConfig | None:
    return parse_provider_fallback_payload(
        raw_provider_fallback,
        source="runtime config field 'provider_fallback'",
    )


def _parse_optional_bool(raw_value: object, *, field_path: str) -> bool | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, bool):
        raise ValueError(f"runtime config field '{field_path}' must be a boolean when provided")
    return raw_value


def _parse_object_container(raw_value: object, *, field_path: str) -> dict[str, object] | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError(f"runtime config field '{field_path}' must be an object when provided")
    return cast(dict[str, object], raw_value)


def _format_runtime_config_field_error(field_path: str) -> str:
    runtime_field_prefix = "runtime config field '"
    if field_path.startswith(runtime_field_prefix):
        if field_path.endswith("'"):
            return field_path
        if "'[" in field_path:
            base, suffix = field_path[len(runtime_field_prefix) :].split("'[", maxsplit=1)
            return f"{runtime_field_prefix}{base}[{suffix}'"
    return f"runtime config field '{field_path}'"


def _parse_string_list(raw_value: object, *, field_path: str) -> tuple[str, ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise ValueError(
            f"{_format_runtime_config_field_error(field_path)} must be an array when provided"
        )

    raw_items = cast(list[object], raw_value)
    parsed_items: list[str] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, str):
            raise ValueError(
                f"{_format_runtime_config_field_error(f'{field_path}[{index}]')} must be a string"
            )
        parsed_items.append(item)
    return tuple(parsed_items)


def _parse_command_list(raw_value: object, *, field_path: str) -> tuple[tuple[str, ...], ...]:
    if raw_value is None:
        return ()
    if not isinstance(raw_value, list):
        raise ValueError(f"runtime config field '{field_path}' must be an array when provided")

    raw_commands = cast(list[object], raw_value)
    parsed_commands: list[tuple[str, ...]] = []
    for command_index, raw_command in enumerate(raw_commands):
        if not isinstance(raw_command, list):
            raise ValueError(
                f"runtime config field '{field_path}[{command_index}]' must be an array"
            )
        command_field_path = f"{field_path}[{command_index}]"
        parsed_command = _parse_string_list(
            cast(list[object], raw_command),
            field_path=command_field_path,
        )
        if not parsed_command:
            raise ValueError(
                f"runtime config field '{command_field_path}' must contain at least one string"
            )
        parsed_commands.append(parsed_command)
    return tuple(parsed_commands)


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
    parsed_environment = _parse_approval_mode(
        environment,
        source=f"environment variable {APPROVAL_MODE_ENV_VAR}",
        allow_none=True,
    )
    if parsed_environment is not None:
        return parsed_environment
    return "ask"


def _resolve_model(
    *, explicit: str | None, repo_local: str | None, environment: str | None
) -> str | None:
    if explicit is not None:
        return explicit
    if repo_local is not None:
        return repo_local
    if environment is not None:
        if not environment:
            raise ValueError(f"environment variable {MODEL_ENV_VAR} must be a non-empty string")
        return environment
    return None


def _resolve_execution_engine(*, repo_local: ExecutionEngineName | None) -> ExecutionEngineName:
    if repo_local is not None:
        return repo_local
    return "deterministic"


def _resolve_max_steps(*, repo_local: int | None) -> int:
    if repo_local is not None:
        return repo_local
    return RuntimeConfig().max_steps


def _parse_approval_mode(
    raw_value: object,
    *,
    source: str,
    allow_none: bool,
) -> PermissionDecision | None:
    if raw_value is None and allow_none:
        return None
    if raw_value not in _VALID_APPROVAL_MODES:
        allowed = ", ".join(_VALID_APPROVAL_MODES)
        raise ValueError(f"{source} must be one of: {allowed}")
    return raw_value


def _parse_execution_engine(
    raw_value: object,
    *,
    source: str,
    allow_none: bool,
) -> ExecutionEngineName | None:
    if raw_value is None and allow_none:
        return None
    if raw_value not in _VALID_EXECUTION_ENGINES:
        allowed = ", ".join(_VALID_EXECUTION_ENGINES)
        raise ValueError(f"{source} must be one of: {allowed}")
    return raw_value


def _parse_max_steps(raw_value: object, *, source: str, allow_none: bool) -> int | None:
    if raw_value is None and allow_none:
        return None
    if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value < 1:
        raise ValueError(f"{source} must be an integer greater than or equal to 1")
    return raw_value
