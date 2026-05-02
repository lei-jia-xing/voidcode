from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

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
        return tuple(
            manifest for manifest in self.list_manifests() if manifest.top_level_selectable
        )

    def executable_primary_ids(self) -> frozenset[str]:
        return frozenset(
            manifest.id
            for manifest in self.list_manifests()
            if manifest.mode == "primary" and manifest.top_level_selectable
        )

    def executable_subagent_ids(self) -> frozenset[str]:
        return frozenset(
            manifest.id for manifest in self.list_manifests() if manifest.mode == "subagent"
        )


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
                f"custom agent manifest {manifest.source_path} uses builtin id "
                f"'{manifest.id}'; builtin agent manifests cannot be replaced"
            )
        existing = custom.get(manifest.id)
        if existing is not None and existing.source_scope == manifest.source_scope:
            raise ValueError(
                "duplicate custom agent manifest id "
                f"'{manifest.id}' in {existing.source_path} and {manifest.source_path}"
            )
        custom[manifest.id] = manifest
    return AgentManifestRegistry(builtin=builtin, custom=custom)


def manifest_from_markdown_file(path: Path, *, scope: AgentSourceScope) -> AgentManifest:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to read custom agent manifest {path}: {exc}") from exc
    try:
        frontmatter, body = _split_frontmatter(content)
        payload = _parse_frontmatter(frontmatter, path=path)
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
            raise ValueError(
                f"duplicate custom agent manifest id '{manifest.id}' in {existing_path} and {path}"
            )
        seen[manifest.id] = path
        manifests.append(manifest)
    return tuple(manifests)


def _split_frontmatter(content: str) -> tuple[str, str]:
    if not content.startswith("---\n"):
        raise ValueError("markdown manifest must start with YAML frontmatter delimiter '---'")
    closing_index = content.find("\n---", 4)
    if closing_index == -1:
        raise ValueError("markdown manifest must close YAML frontmatter with '---'")
    frontmatter = content[4:closing_index]
    body = content[closing_index + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    if not body.strip():
        raise ValueError("markdown manifest body prompt must be non-empty")
    return frontmatter, body.strip()


def _parse_frontmatter(frontmatter: str, *, path: Path) -> dict[str, object]:
    payload: dict[str, object] = {}
    lines = frontmatter.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            raise ValueError(f"unexpected indented frontmatter line: {line!r}")
        key, separator, raw_value = line.partition(":")
        if separator != ":" or not key.strip():
            raise ValueError(f"frontmatter line must use 'key: value' syntax: {line!r}")
        normalized_key = key.strip()
        if normalized_key not in _SUPPORTED_FRONTMATTER_FIELDS:
            supported = ", ".join(sorted(_SUPPORTED_FRONTMATTER_FIELDS))
            raise ValueError(
                f"unsupported frontmatter field '{normalized_key}'; supported fields are: "
                f"{supported}"
            )
        if normalized_key in payload:
            raise ValueError(f"duplicate frontmatter field '{normalized_key}'")
        value_text = raw_value.strip()
        if value_text in {"|", ">"}:
            collected: list[str] = []
            while index < len(lines) and lines[index].startswith((" ", "\t")):
                collected.append(lines[index])
                index += 1
            if not collected:
                raise ValueError(f"frontmatter field '{normalized_key}' must declare a value")
            payload[normalized_key] = _parse_scalar_block(collected, folded=value_text == ">")
            continue
        if not value_text:
            collected: list[str] = []
            while index < len(lines) and lines[index].startswith((" ", "\t")):
                collected.append(lines[index])
                index += 1
            if not collected:
                raise ValueError(f"frontmatter field '{normalized_key}' must declare a value")
            payload[normalized_key] = _parse_block_value(collected, field=normalized_key, path=path)
        else:
            payload[normalized_key] = _parse_inline_value(value_text)
    missing = sorted(field for field in _REQUIRED_FRONTMATTER_FIELDS if field not in payload)
    if missing:
        raise ValueError(f"missing required frontmatter field(s): {', '.join(missing)}")
    return payload


def _parse_inline_value(value_text: str) -> object:
    if value_text.startswith("[") and value_text.endswith("]"):
        inner = value_text[1:-1].strip()
        if not inner:
            return []
        return [_strip_quotes(item.strip()) for item in inner.split(",")]
    return _strip_quotes(value_text)


def _parse_scalar_block(lines: list[str], *, folded: bool) -> str:
    non_empty_lines = [line for line in lines if line.strip()]
    if not non_empty_lines:
        return ""
    min_indent = min(len(line) - len(line.lstrip(" \t")) for line in non_empty_lines)
    normalized = [line[min_indent:] if len(line) >= min_indent else "" for line in lines]
    if folded:
        return " ".join(line.strip() for line in normalized if line.strip()).strip()
    return "\n".join(normalized).strip()


def _parse_block_value(lines: list[str], *, field: str, path: Path) -> object:
    items: list[str] = []
    mapping: dict[str, object] = {}
    active_mapping_key: str | None = None
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            items.append(_strip_quotes(stripped[1:].strip()))
            active_mapping_key = None
            continue
        key, separator, value = stripped.partition(":")
        if separator == ":":
            normalized_key = key.strip()
            raw_value = value.strip()
            if raw_value:
                mapping[normalized_key] = _parse_inline_value(raw_value)
                active_mapping_key = None
            else:
                mapping[normalized_key] = []
                active_mapping_key = normalized_key
            continue
        if active_mapping_key is not None and stripped.startswith("-"):
            cast(list[str], mapping[active_mapping_key]).append(_strip_quotes(stripped[1:].strip()))
            continue
        raise ValueError(f"frontmatter field '{field}' has unsupported block syntax in {path}")
    if items and mapping:
        raise ValueError(f"frontmatter field '{field}' cannot mix list and object syntax")
    return items if items else mapping


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _manifest_from_payload(
    payload: Mapping[str, object],
    *,
    body: str,
    path: Path,
    scope: AgentSourceScope,
) -> AgentManifest:
    name = _required_string(payload, "name")
    manifest_id = _optional_string(payload, "id") or agent_manifest_id_from_name(name)
    if not is_valid_agent_manifest_id(manifest_id):
        raise ValueError(
            f"frontmatter field 'id' value '{manifest_id}' must match {_AGENT_ID_PATTERN.pattern!r}"
        )
    mode = _parse_mode(_required_string(payload, "mode"))
    tool_allowlist = _string_list(payload.get("tool_allowlist"), field="tool_allowlist")
    skill_refs = _string_list(payload.get("skill_refs"), field="skill_refs")
    preset_hook_refs = validate_hook_preset_refs(
        _string_list(payload.get("preset_hook_refs"), field="preset_hook_refs"),
        field_path=f"custom agent manifest {path} preset_hook_refs",
    )
    prompt_append = _optional_string(payload, "prompt_append")
    return AgentManifest(
        id=manifest_id,
        name=name,
        mode=mode,
        description=_required_string(payload, "description"),
        source_scope=scope,
        source_path=str(path),
        prompt_profile=manifest_id,
        execution_engine="provider",
        model_preference=_optional_string(payload, "model"),
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


def _optional_string(payload: Mapping[str, object], field: str) -> str | None:
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
    for index, item in enumerate(cast(list[object], value)):
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
    payload = cast(dict[str, object], value)
    unknown = sorted(key for key in payload if key not in {"profile", "servers"})
    if unknown:
        raise ValueError(
            f"frontmatter field 'mcp_binding' has unsupported key(s): {', '.join(unknown)}"
        )
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
        raise ValueError(
            f"custom agent manifest{source} uses builtin id '{agent_id}'; "
            "builtin agent manifests cannot be replaced"
        )


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
