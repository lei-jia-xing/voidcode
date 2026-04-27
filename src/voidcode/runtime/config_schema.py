"""Schema-backed config UX for the VoidCode runtime."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .config import (
    APPROVAL_MODE_ENV_VAR,
    EXECUTION_ENGINE_ENV_VAR,
    MAX_STEPS_ENV_VAR,
    MODEL_ENV_VAR,
    RUNTIME_CONFIG_FILE_NAME,
    TOOL_TIMEOUT_ENV_VAR,
    runtime_config_path,
)

__all__ = [
    "RUNTIME_CONFIG_SCHEMA_ID",
    "RUNTIME_CONFIG_SCHEMA_TITLE",
    "RUNTIME_CONFIG_SCHEMA_URI",
    "ConfigMigration",
    "MigrationAction",
    "apply_config_migrations",
    "detect_config_migrations",
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

MigrationAction = Literal["remove", "rename"]


@dataclass(frozen=True, slots=True)
class ConfigMigration:
    field_path: tuple[str, ...]
    action: MigrationAction
    reason: str
    new_field_path: tuple[str, ...] | None = None

    @property
    def display_path(self) -> str:
        return ".".join(self.field_path)

    @property
    def display_new_path(self) -> str | None:
        if self.new_field_path is None:
            return None
        return ".".join(self.new_field_path)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "field_path": self.display_path,
            "action": self.action,
            "reason": self.reason,
        }
        if self.new_field_path is not None:
            payload["new_field_path"] = self.display_new_path
        return payload


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
            f"{TOOL_TIMEOUT_ENV_VAR}) and the user-level "
            "`~/.config/voidcode/config.json`."
        ),
        "type": "object",
        "additionalProperties": True,
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
                "description": "Maximum graph step budget for a single run.",
            },
            "tool_timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "description": "Timeout applied to each tool execution.",
            },
            "hooks": {
                "type": "object",
                "additionalProperties": True,
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
                    "on_background_task_completed": {"$ref": "#/$defs/commandList"},
                    "on_background_task_failed": {"$ref": "#/$defs/commandList"},
                    "on_background_task_cancelled": {"$ref": "#/$defs/commandList"},
                    "on_delegated_result_available": {"$ref": "#/$defs/commandList"},
                    "formatter_presets": {
                        "type": "object",
                        "additionalProperties": {"type": "object"},
                    },
                },
            },
            "tools": {"$ref": "#/$defs/toolsConfig"},
            "skills": {"$ref": "#/$defs/skillsConfig"},
            "lsp": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "servers": {
                        "type": "object",
                        "additionalProperties": {"type": "object"},
                    },
                },
            },
            "mcp": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "request_timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                    "servers": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "additionalProperties": True,
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
                            },
                        },
                    },
                },
            },
            "tui": {
                "type": "object",
                "additionalProperties": True,
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
                        "additionalProperties": True,
                        "properties": {
                            "theme": {
                                "type": "object",
                                "additionalProperties": True,
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
                                "additionalProperties": True,
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
                "additionalProperties": True,
                "description": "Provider fallback chain configuration.",
            },
            "providers": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Provider-level configuration. Credential fields are sensitive; "
                    "prefer environment variables for secrets."
                ),
            },
            "plan": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "provider": {"type": "string", "minLength": 1},
                    "module": {"type": "string", "minLength": 1},
                    "factory": {"type": "string", "minLength": 1},
                    "options": {"type": "object", "additionalProperties": True},
                },
            },
            "agent": {"$ref": "#/$defs/agentConfig"},
            "agents": {
                "type": "object",
                "additionalProperties": {"$ref": "#/$defs/agentConfig"},
                "propertyNames": {"pattern": "^[a-z][a-z0-9_-]*$"},
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
            "toolsConfig": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "builtin": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {"enabled": {"type": "boolean"}},
                    },
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "allowlist": {"type": "array", "items": {"type": "string"}},
                    "default": {"type": "array", "items": {"type": "string"}},
                },
            },
            "skillsConfig": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "agentConfig": {
                "type": "object",
                "additionalProperties": True,
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
                        "additionalProperties": True,
                    },
                },
            },
        },
    }


def generate_starter_runtime_config(
    *,
    approval_mode: str = "ask",
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

    payload: dict[str, object] = {}
    if include_schema_reference:
        payload["$schema"] = RUNTIME_CONFIG_SCHEMA_ID
    payload["approval_mode"] = approval_mode
    if execution_engine is not None:
        payload["execution_engine"] = execution_engine
    if max_steps is not None:
        payload["max_steps"] = max_steps
    if include_examples:
        payload["tools"] = {"builtin": {"enabled": True}}
        payload["skills"] = {"enabled": True}
    return payload


def format_starter_runtime_config_json(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n"


def detect_config_migrations(payload: object) -> tuple[ConfigMigration, ...]:
    if not isinstance(payload, dict):
        return ()
    typed_payload = cast(dict[str, object], payload)

    migrations: list[ConfigMigration] = []

    raw_agent = typed_payload.get("agent")
    if isinstance(raw_agent, dict):
        agent_payload = cast(dict[str, object], raw_agent)
        if "leader_mode" in agent_payload:
            migrations.append(
                ConfigMigration(
                    field_path=("agent", "leader_mode"),
                    action="remove",
                    reason=(
                        "agent.leader_mode has been removed; "
                        "use the default leader execution flow instead"
                    ),
                )
            )

    return tuple(migrations)


def apply_config_migrations(
    payload: Mapping[str, object],
    migrations: Sequence[ConfigMigration],
) -> dict[str, object]:
    new_payload = _clone_payload(payload)
    for migration in migrations:
        if migration.action == "remove":
            _delete_path(new_payload, migration.field_path)
        elif migration.action == "rename":
            if migration.new_field_path is None:
                raise ValueError(
                    f"rename migration requires a new_field_path; received {migration!r}"
                )
            value = _pop_path(new_payload, migration.field_path)
            if value is _MISSING:
                continue
            _set_path(new_payload, migration.new_field_path, value)
    return new_payload


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


_MISSING: object = object()


def _clone_payload(payload: Mapping[str, object]) -> dict[str, object]:
    return cast(dict[str, object], json.loads(json.dumps(dict(payload))))


def _delete_path(payload: dict[str, object], path: Sequence[str]) -> None:
    if not path:
        return
    parent = _resolve_parent(payload, path, create_missing=False)
    if parent is None:
        return
    parent.pop(path[-1], None)


def _pop_path(payload: dict[str, object], path: Sequence[str]) -> object:
    if not path:
        return _MISSING
    parent = _resolve_parent(payload, path, create_missing=False)
    if parent is None or path[-1] not in parent:
        return _MISSING
    return parent.pop(path[-1])


def _set_path(payload: dict[str, object], path: Sequence[str], value: object) -> None:
    if not path:
        raise ValueError("set path requires at least one segment")
    parent = _resolve_parent(payload, path, create_missing=True)
    if parent is None:
        raise ValueError("could not resolve parent path for rename target: " + ".".join(path))
    parent[path[-1]] = value


def _resolve_parent(
    payload: dict[str, object],
    path: Sequence[str],
    *,
    create_missing: bool,
) -> dict[str, object] | None:
    current: dict[str, object] = payload
    for segment in path[:-1]:
        nxt = current.get(segment)
        if isinstance(nxt, dict):
            current = cast(dict[str, object], nxt)
            continue
        if not create_missing:
            return None
        new_child: dict[str, object] = {}
        current[segment] = new_child
        current = new_child
    return current
