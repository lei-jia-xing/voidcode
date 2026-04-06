from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .permission import PermissionDecision

RUNTIME_CONFIG_FILE_NAME = ".voidcode.json"
APPROVAL_MODE_ENV_VAR = "VOIDCODE_APPROVAL_MODE"
_VALID_APPROVAL_MODES = ("allow", "deny", "ask")


@dataclass(frozen=True, slots=True)
class RuntimeHooksConfig:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    approval_mode: PermissionDecision = "ask"
    model: str | None = None
    hooks: RuntimeHooksConfig | None = None


@dataclass(frozen=True, slots=True)
class RuntimeConfigOverrides:
    approval_mode: PermissionDecision | None = None
    model: str | None = None
    hooks: RuntimeHooksConfig | None = None


def runtime_config_path(workspace: Path) -> Path:
    return workspace / RUNTIME_CONFIG_FILE_NAME


def load_runtime_config(
    workspace: Path,
    *,
    approval_mode: PermissionDecision | None = None,
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
        model=repo_local.model,
        hooks=repo_local.hooks,
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

    raw_hooks = payload.get("hooks")
    hooks = _parse_hooks_config(raw_hooks)

    raw_approval_mode = payload.get("approval_mode")
    parsed_approval_mode = _parse_approval_mode(
        raw_approval_mode,
        source=f"runtime config field 'approval_mode' in {config_path}",
        allow_none=True,
    )

    return RuntimeConfigOverrides(
        approval_mode=parsed_approval_mode,
        model=raw_model,
        hooks=hooks,
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

    return RuntimeHooksConfig(enabled=enabled)


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
