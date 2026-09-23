from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..frontmatter import load_frontmatter_mapping, split_frontmatter
from ..hook.presets import validate_hook_preset_refs
from .builtin import get_builtin_agent_manifest, list_builtin_agent_manifests
from .models import (
    AgentManifest,
    AgentMcpBindingIntent,
    AgentMode,
    AgentPromptMaterialization,
    AgentSourceScope,
)

_AGENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def _running_on_windows() -> bool:
    return sys.platform == "win32"


_SUPPORTED_FRONTMATTER_FIELDS = frozenset(
    {
        "id",
        "name",
        "description",
        "mode",
        "model",
        "fallback_models",
        "tool_allowlist",
        "skill_refs",
        "preset_hook_refs",
        "mcp_binding",
        "prompt_append",
    }
)
_REQUIRED_FRONTMATTER_FIELDS = frozenset({"name", "description", "mode"})


@dataclass(frozen=True, slots=True)
class AgentManifestRegistry:
    builtin: Mapping[str, AgentManifest]
    custom: Mapping[str, AgentManifest]

    def get(self, agent_id: str) -> AgentManifest | None:
        return self.custom.get(agent_id) or self.builtin.get(agent_id)

    def list_manifests(self) -> tuple[AgentManifest, ...]:
        return (*self.builtin.values(), *self.custom.values())

    def list_top_level_selectable(self) -> tuple[AgentManifest, ...]:
        return tuple(manifest for manifest in self.list_manifests() if manifest.top_level_selectable)

    def executable_primary_ids(self) -> frozenset[str]:
        return frozenset(manifest.id for manifest in self.list_manifests() if manifest.mode == "primary" and manifest.top_level_selectable)

    def executable_subagent_ids(self) -> frozenset[str]:
        return frozenset(manifest.id for manifest in self.list_manifests() if manifest.mode == "subagent")


def agent_manifest_id_from_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if not normalized:
        raise ValueError("agent manifest name must contain at least one alphanumeric character")
    return normalized


def is_valid_agent_manifest_id(agent_id: str) -> bool:
    return bool(_AGENT_ID_PATTERN.fullmatch(agent_id))


def user_agent_manifest_dir(env: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    if _running_on_windows():
        config_home = environment.get("APPDATA") or os.environ.get("APPDATA")
        if config_home:
            return Path(config_home).expanduser() / "voidcode" / "agents"
        local_config_home = environment.get("LOCALAPPDATA") or os.environ.get("LOCALAPPDATA")
        if local_config_home:
            return Path(local_config_home).expanduser() / "voidcode" / "agents"
        return Path.home() / "AppData" / "Roaming" / "voidcode" / "agents"

    config_home = environment.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "voidcode" / "agents"
    return Path.home() / ".config" / "voidcode" / "agents"


def project_agent_manifest_dir(workspace: Path) -> Path:
    return workspace.resolve() / ".voidcode" / "agents"


def load_agent_manifest_registry(
    workspace: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> AgentManifestRegistry:
    builtin = {manifest.id: manifest for manifest in list_builtin_agent_manifests()}
    user_manifests = _discover_custom_agent_manifests(
        user_agent_manifest_dir(env),
        scope="user",
    )
    project_manifests = _discover_custom_agent_manifests(
        project_agent_manifest_dir(workspace),
        scope="project",
    )
    custom: dict[str, AgentManifest] = {}
    for manifest in (*user_manifests, *project_manifests):
        if manifest.id in builtin:
            raise ValueError(
                f"custom agent manifest {manifest.source_path} uses builtin id '{manifest.id}'; builtin agent manifests cannot be replaced"
            )
        existing = custom.get(manifest.id)
        if existing is not None and existing.source_scope == manifest.source_scope:
            raise ValueError(f"duplicate custom agent manifest id '{manifest.id}' in {existing.source_path} and {manifest.source_path}")
        custom[manifest.id] = manifest
    return AgentManifestRegistry(builtin=builtin, custom=custom)


def manifest_from_markdown_file(path: Path, *, scope: AgentSourceScope) -> AgentManifest:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to read custom agent manifest {path}: {exc}") from exc
    try:
        frontmatter, body = split_frontmatter(content, require_body=True)
        payload = _validate_frontmatter_fields(load_frontmatter_mapping(frontmatter))
        return _manifest_from_payload(payload, body=body, path=path, scope=scope)
    except ValueError as exc:
        raise ValueError(f"invalid custom agent manifest {path}: {exc}") from exc


def _discover_custom_agent_manifests(
    directory: Path,
    *,
    scope: Literal["project", "user"],
) -> tuple[AgentManifest, ...]:
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ValueError(f"custom agent manifest path must be a directory: {directory}")
    manifests: list[AgentManifest] = []
    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("*.md")):
        manifest = manifest_from_markdown_file(path, scope=scope)
        existing_path = seen.get(manifest.id)
        if existing_path is not None:
            raise ValueError(f"duplicate custom agent manifest id '{manifest.id}' in {existing_path} and {path}")
        seen[manifest.id] = path
        manifests.append(manifest)
    return tuple(manifests)


def _validate_frontmatter_fields(payload: Mapping[str, object]) -> dict[str, object]:
    """Enforce the agent manifest field whitelist and required fields."""

    for field in payload:
        if field not in _SUPPORTED_FRONTMATTER_FIELDS:
            supported = ", ".join(sorted(_SUPPORTED_FRONTMATTER_FIELDS))
            raise ValueError(f"unsupported frontmatter field '{field}'; supported fields are: {supported}")
    missing = sorted(field for field in _REQUIRED_FRONTMATTER_FIELDS if field not in payload)
    if missing:
        raise ValueError(f"missing required frontmatter field(s): {', '.join(missing)}")
    return dict(payload)


def _manifest_from_payload(
    payload: Mapping[str, object],
    *,
    body: str,
    path: Path,
    scope: AgentSourceScope,
) -> AgentManifest:
    name = _required_string(payload, "name")
    manifest_id = _require_optional_string(payload, "id") or agent_manifest_id_from_name(name)
    if not is_valid_agent_manifest_id(manifest_id):
        raise ValueError(f"frontmatter field 'id' value '{manifest_id}' must match {_AGENT_ID_PATTERN.pattern!r}")
    mode = _parse_mode(_required_string(payload, "mode"))
    tool_allowlist = _string_list(payload.get("tool_allowlist"), field="tool_allowlist")
    skill_refs = _string_list(payload.get("skill_refs"), field="skill_refs")
    preset_hook_refs = validate_hook_preset_refs(
        _string_list(payload.get("preset_hook_refs"), field="preset_hook_refs"),
        field_path=f"custom agent manifest {path} preset_hook_refs",
    )
    prompt_append = _require_optional_string(payload, "prompt_append")
    return AgentManifest(
        id=manifest_id,
        name=name,
        mode=mode,
        description=_required_string(payload, "description"),
        source_scope=scope,
        source_path=str(path),
        prompt_profile=manifest_id,
        execution_engine="provider",
        model_preference=_require_optional_string(payload, "model"),
        fallback_models=_string_list(payload.get("fallback_models"), field="fallback_models"),
        tool_allowlist=tool_allowlist,
        skill_refs=skill_refs,
        preset_hook_refs=preset_hook_refs,
        mcp_binding=_parse_mcp_binding(payload.get("mcp_binding")),
        top_level_selectable=mode == "primary",
        prompt_materialization=AgentPromptMaterialization(
            profile=manifest_id,
            version=1,
            source="custom_markdown",
            format="markdown",
            body=body,
            prompt_append=prompt_append,
            source_scope=scope,
            source_path=str(path),
        ),
    )


def _parse_mode(value: str) -> AgentMode:
    if value == "primary":
        return "primary"
    if value == "subagent":
        return "subagent"
    raise ValueError("frontmatter field 'mode' must be 'primary' or 'subagent'")


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"frontmatter field '{field}' must be a non-empty string")
    return value.strip()


def _require_optional_string(payload: Mapping[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"frontmatter field '{field}' must be a non-empty string")
    return value.strip()


def _string_list(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"frontmatter field '{field}' must be a string array")
    parsed: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"frontmatter field '{field}[{index}]' must be a non-empty string")
        parsed.append(item.strip())
    if len(parsed) != len(set(parsed)):
        raise ValueError(f"frontmatter field '{field}' must not contain duplicates")
    return tuple(parsed)


def _parse_mcp_binding(value: object) -> AgentMcpBindingIntent | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("frontmatter field 'mcp_binding' must be an object")
    payload = value
    unknown = sorted(key for key in payload if key not in {"profile", "servers"})
    if unknown:
        raise ValueError(f"frontmatter field 'mcp_binding' has unsupported key(s): {', '.join(unknown)}")
    profile = payload.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        raise ValueError("frontmatter field 'mcp_binding.profile' must be a non-empty string")
    return AgentMcpBindingIntent(
        profile=profile.strip() if isinstance(profile, str) else None,
        servers=_string_list(payload.get("servers"), field="mcp_binding.servers"),
    )


def assert_not_builtin_agent_id(agent_id: str, *, source_path: str | None = None) -> None:
    if get_builtin_agent_manifest(agent_id) is not None:
        source = f" in {source_path}" if source_path else ""
        raise ValueError(f"custom agent manifest{source} uses builtin id '{agent_id}'; builtin agent manifests cannot be replaced")


__all__ = [
    "AgentManifestRegistry",
    "agent_manifest_id_from_name",
    "assert_not_builtin_agent_id",
    "is_valid_agent_manifest_id",
    "load_agent_manifest_registry",
    "manifest_from_markdown_file",
    "project_agent_manifest_dir",
    "user_agent_manifest_dir",
]
