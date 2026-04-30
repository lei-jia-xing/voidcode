"""Schema-backed config UX for the VoidCode runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from .config import (
    APPROVAL_MODE_ENV_VAR,
    EXECUTION_ENGINE_ENV_VAR,
    MAX_STEPS_ENV_VAR,
    MODEL_ENV_VAR,
    REASONING_EFFORT_ENV_VAR,
    RUNTIME_CONFIG_FILE_NAME,
    TOOL_TIMEOUT_ENV_VAR,
    runtime_config_path,
)

__all__ = [
    "RUNTIME_CONFIG_SCHEMA_ID",
    "RUNTIME_CONFIG_SCHEMA_TITLE",
    "RUNTIME_CONFIG_SCHEMA_URI",
    "format_starter_runtime_config_json",
    "generate_starter_runtime_config",
    "read_runtime_config_payload",
    "runtime_config_json_schema",
    "write_runtime_config_payload",
]

RUNTIME_CONFIG_SCHEMA_ID = "https://voidcode.dev/schemas/runtime-config.schema.json"
RUNTIME_CONFIG_SCHEMA_URI = RUNTIME_CONFIG_SCHEMA_ID
RUNTIME_CONFIG_SCHEMA_TITLE = "VoidCode runtime config"
_JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def runtime_config_json_schema() -> dict[str, object]:
    return {
        "$schema": _JSON_SCHEMA_DRAFT,
        "$id": RUNTIME_CONFIG_SCHEMA_ID,
        "title": RUNTIME_CONFIG_SCHEMA_TITLE,
        "description": (
            "Workspace-local VoidCode runtime configuration. Stored at "
            f"`{RUNTIME_CONFIG_FILE_NAME}` in the workspace root. "
            "Resolves alongside environment variables "
            f"({APPROVAL_MODE_ENV_VAR}, {MODEL_ENV_VAR}, "
            f"{EXECUTION_ENGINE_ENV_VAR}, {MAX_STEPS_ENV_VAR}, "
            f"{TOOL_TIMEOUT_ENV_VAR}, {REASONING_EFFORT_ENV_VAR}) and the user-level "
            "`~/.config/voidcode/config.json`."
        ),
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "$schema": {
                "type": "string",
                "description": "JSON Schema reference for editor support.",
            },
            "approval_mode": {
                "type": "string",
                "enum": ["allow", "deny", "ask"],
                "description": "Default approval policy for tool execution.",
            },
            "permission": {"$ref": "#/$defs/permissionConfig"},
            "model": {
                "type": "string",
                "minLength": 1,
                "description": "Provider/model identifier in `provider/model` form.",
            },
            "execution_engine": {
                "type": "string",
                "enum": ["deterministic", "provider"],
                "description": "Execution engine used to advance graph steps.",
            },
            "max_steps": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Maximum graph step budget for a single run. Omit this field for "
                    "the provider default of no fixed step cap."
                ),
            },
            "tool_timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": "Timeout applied to each tool execution.",
            },
            "reasoning_effort": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Optional runtime-owned reasoning-effort hint forwarded to the active "
                    "provider when supported (for example, 'low', 'medium', 'high'). Runtime "
                    "rejects this hint when the resolved model explicitly does not support "
                    "reasoning effort."
                ),
            },
            "hooks": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "Runtime-managed lifecycle hooks (pre/post tool, session, background)."
                ),
                "properties": {
                    "enabled": {"type": "boolean"},
                    "timeout_seconds": {"type": "number", "minimum": 1},
                    "pre_tool": {"$ref": "#/$defs/commandList"},
                    "post_tool": {"$ref": "#/$defs/commandList"},
                    "on_session_start": {"$ref": "#/$defs/commandList"},
                    "on_session_end": {"$ref": "#/$defs/commandList"},
                    "on_session_idle": {"$ref": "#/$defs/commandList"},
                    "on_background_task_registered": {"$ref": "#/$defs/commandList"},
                    "on_background_task_started": {"$ref": "#/$defs/commandList"},
                    "on_background_task_progress": {"$ref": "#/$defs/commandList"},
                    "on_background_task_completed": {"$ref": "#/$defs/commandList"},
                    "on_background_task_failed": {"$ref": "#/$defs/commandList"},
                    "on_background_task_cancelled": {"$ref": "#/$defs/commandList"},
                    "on_background_task_notification_enqueued": {"$ref": "#/$defs/commandList"},
                    "on_background_task_result_read": {"$ref": "#/$defs/commandList"},
                    "on_delegated_result_available": {"$ref": "#/$defs/commandList"},
                    "on_context_pressure": {"$ref": "#/$defs/commandList"},
                    "formatter_presets": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/$defs/formatterPresetConfig"},
                    },
                },
            },
            "tools": {"$ref": "#/$defs/toolsConfig"},
            "skills": {"$ref": "#/$defs/skillsConfig"},
            "context_window": {"$ref": "#/$defs/contextWindowConfig"},
            "lsp": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "servers": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/$defs/lspServerConfig"},
                    },
                },
            },
            "mcp": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "request_timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                    "servers": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["command"],
                            "properties": {
                                "transport": {"type": "string", "enum": ["stdio"]},
                                "command": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "minItems": 1,
                                },
                                "env": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                },
                                "scope": {
                                    "type": "string",
                                    "enum": ["runtime", "session"],
                                    "description": (
                                        "Runtime-scoped servers are shared by the runtime; "
                                        "session-scoped servers are isolated per session."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
            "tui": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "leader_key": {"type": "string"},
                    "keymap": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "string",
                            "enum": [
                                "command_palette",
                                "session_new",
                                "session_resume",
                            ],
                        },
                    },
                    "preferences": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "theme": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string"},
                                    "mode": {
                                        "type": "string",
                                        "enum": ["auto", "light", "dark"],
                                    },
                                },
                            },
                            "reading": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "wrap": {"type": "boolean"},
                                    "sidebar_collapsed": {"type": "boolean"},
                                },
                            },
                        },
                    },
                },
            },
            "provider_fallback": {
                "type": "object",
                "additionalProperties": False,
                "description": "Provider fallback chain configuration.",
                "properties": {
                    "preferred_model": {"type": "string", "minLength": 1},
                    "fallback_models": {"type": "array", "items": {"type": "string"}},
                },
            },
            "providers": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Provider-level configuration. Credential fields are sensitive; "
                    "prefer environment variables for secrets."
                ),
            },
            "background_task": {
                "type": "object",
                "additionalProperties": True,
                "description": "Background task queue and concurrency limits.",
                "properties": {
                    "default_concurrency": {"type": "integer", "minimum": 1},
                    "provider_concurrency": {
                        "type": "object",
                        "additionalProperties": {"type": "integer", "minimum": 1},
                    },
                    "model_concurrency": {
                        "type": "object",
                        "additionalProperties": {"type": "integer", "minimum": 1},
                    },
                },
            },
            "agent": {"$ref": "#/$defs/agentConfig"},
            "agents": {
                "type": "object",
                "properties": {
                    "leader": {"$ref": "#/$defs/agentConfig"},
                    "worker": {"$ref": "#/$defs/agentConfig"},
                    "advisor": {"$ref": "#/$defs/agentConfig"},
                    "explore": {"$ref": "#/$defs/agentConfig"},
                    "researcher": {"$ref": "#/$defs/agentConfig"},
                    "product": {"$ref": "#/$defs/agentConfig"},
                },
                "additionalProperties": {"$ref": "#/$defs/customAgentConfig"},
                "propertyNames": {"pattern": "^[a-z][a-z0-9_-]*$"},
            },
            "categories": {
                "type": "object",
                "description": "Per task-category runtime model overrides for delegated sessions.",
                "additionalProperties": {"$ref": "#/$defs/categoryConfig"},
                "propertyNames": {
                    "enum": [
                        "brain",
                        "deep",
                        "high",
                        "low",
                        "quick",
                        "visual-engineering",
                        "writing",
                    ]
                },
            },
        },
        "$defs": {
            "commandList": {
                "type": "array",
                "description": "Array of commands; each command is a non-empty array of strings.",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "formatterPresetConfig": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "root_markers": {"type": "array", "items": {"type": "string"}},
                    "fallback_commands": {"$ref": "#/$defs/commandList"},
                    "cwd_policy": {
                        "type": "string",
                        "enum": ["workspace", "nearest_root", "file_directory"],
                    },
                },
            },
            "toolsConfig": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "builtin": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"enabled": {"type": "boolean"}},
                    },
                    "allowlist": {"type": "array", "items": {"type": "string"}},
                    "default": {"type": "array", "items": {"type": "string"}},
                },
            },
            "contextWindowConfig": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "version": {"type": "integer", "const": 1},
                    "auto_compaction": {"type": "boolean"},
                    "max_tool_results": {"type": "integer", "minimum": 1},
                    "max_tool_result_tokens": {"type": "integer", "minimum": 1},
                    "max_context_ratio": {"type": "number", "exclusiveMinimum": 0},
                    "model_context_window_tokens": {"type": "integer", "minimum": 1},
                    "reserved_output_tokens": {"type": "integer", "minimum": 1},
                    "minimum_retained_tool_results": {"type": "integer", "minimum": 1},
                    "recent_tool_result_count": {"type": "integer", "minimum": 1},
                    "recent_tool_result_tokens": {"type": "integer", "minimum": 1},
                    "default_tool_result_tokens": {"type": "integer", "minimum": 1},
                    "per_tool_result_tokens": {
                        "type": "object",
                        "additionalProperties": {"type": "integer", "minimum": 1},
                    },
                    "tokenizer_model": {"type": "string", "minLength": 1},
                    "continuity_preview_items": {"type": "integer", "minimum": 1},
                    "continuity_preview_chars": {"type": "integer", "minimum": 1},
                    "context_pressure_threshold": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1,
                    },
                    "context_pressure_cooldown_steps": {"type": "integer", "minimum": 1},
                },
            },
            "lspServerConfig": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "preset": {"type": "string", "minLength": 1},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "languages": {"type": "array", "items": {"type": "string"}},
                    "extensions": {"type": "array", "items": {"type": "string"}},
                    "root_markers": {"type": "array", "items": {"type": "string"}},
                    "settings": {"type": "object"},
                    "init_options": {"type": "object"},
                },
            },
            "skillsConfig": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "agentConfig": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "preset": {
                        "type": "string",
                        "enum": [
                            "leader",
                            "worker",
                            "advisor",
                            "explore",
                            "researcher",
                            "product",
                        ],
                    },
                    "prompt_profile": {"type": "string", "minLength": 1},
                    "prompt_materialization": {"type": "object"},
                    "prompt_ref": {"type": "string", "minLength": 1},
                    "prompt_source": {"type": "string", "enum": ["builtin"]},
                    "hook_refs": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string", "minLength": 1},
                    "execution_engine": {
                        "type": "string",
                        "enum": ["deterministic", "provider"],
                    },
                    "tools": {"$ref": "#/$defs/toolsConfig"},
                    "skills": {"$ref": "#/$defs/skillsConfig"},
                    "provider_fallback": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "preferred_model": {"type": "string", "minLength": 1},
                            "fallback_models": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "fallback_models": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "description": (
                            "Agent-scoped shorthand for provider_fallback.fallback_models; "
                            "requires agent.model as the preferred model."
                        ),
                    },
                },
            },
            "categoryConfig": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "model": {"type": "string", "minLength": 1},
                },
            },
            "permissionConfig": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "external_directory_read": {"$ref": "#/$defs/permissionRules"},
                    "external_directory_write": {"$ref": "#/$defs/permissionRules"},
                },
            },
            "permissionRules": {
                "type": "object",
                "description": "Ordered path-glob permission map. First matching pattern applies.",
                "additionalProperties": {
                    "type": "string",
                    "enum": ["allow", "deny", "ask"],
                },
            },
            "customAgentConfig": {
                "allOf": [{"$ref": "#/$defs/agentConfig"}],
                "required": ["preset"],
            },
        },
    }


def generate_starter_runtime_config(
    *,
    approval_mode: str = "ask",
    model: str | None = None,
    execution_engine: str | None = None,
    max_steps: int | None = None,
    include_examples: bool = False,
    include_schema_reference: bool = True,
) -> dict[str, object]:
    if approval_mode not in {"allow", "deny", "ask"}:
        raise ValueError(
            f"approval_mode must be one of: allow, deny, ask; received {approval_mode!r}"
        )
    if execution_engine is not None and execution_engine not in {
        "deterministic",
        "provider",
    }:
        raise ValueError(
            "execution_engine must be one of: deterministic, provider; received "
            f"{execution_engine!r}"
        )
    if max_steps is not None and max_steps < 1:
        raise ValueError("max_steps must be an integer greater than or equal to 1")
    if model is not None:
        _validate_model_reference(model)
    if execution_engine == "provider" and model is None:
        raise ValueError(
            "execution_engine provider requires model; pass --model provider/model "
            "or omit execution_engine to use runtime defaults"
        )

    payload: dict[str, object] = {}
    if include_schema_reference:
        payload["$schema"] = RUNTIME_CONFIG_SCHEMA_ID
    payload["approval_mode"] = approval_mode
    if model is not None:
        payload["model"] = model
    if execution_engine is not None:
        payload["execution_engine"] = execution_engine
    if max_steps is not None:
        payload["max_steps"] = max_steps
    if include_examples:
        payload["tools"] = {"builtin": {"enabled": True}}
        payload["skills"] = {"enabled": True}
    return payload


def _validate_model_reference(model: str) -> None:
    provider_name, separator, model_name = model.partition("/")
    if separator != "/" or not provider_name or not model_name:
        raise ValueError("model must use provider/model format")


def format_starter_runtime_config_json(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n"


def read_runtime_config_payload(workspace: Path) -> dict[str, object] | None:
    config_path = runtime_config_path(workspace.resolve())
    if not config_path.exists():
        return None
    raw_text = config_path.read_text(encoding="utf-8")
    try:
        raw_payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime config file must contain valid JSON: {config_path}") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError(f"runtime config file must contain a JSON object: {config_path}")
    return cast(dict[str, object], raw_payload)


def write_runtime_config_payload(
    workspace: Path,
    payload: Mapping[str, object],
    *,
    create_parents: bool = True,
) -> Path:
    config_path = runtime_config_path(workspace.resolve())
    if create_parents:
        config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        format_starter_runtime_config_json(payload),
        encoding="utf-8",
    )
    return config_path
