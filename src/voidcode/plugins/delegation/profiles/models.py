"""Agent manifest value records, ported verbatim from ``voidcode.agent.models``.

`AgentManifest` is the discovered profile record; `AgentPromptMaterialization`
describes how that profile's prompt is rendered; `AgentMcpBindingIntent` is
declarative binding intent only, never execution authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

type AgentManifestId = Literal[
    "default",
    "execute",
    "review",
    "explore",
    "research",
    "plan",
]
type AgentManifestKey = AgentManifestId | str
type AgentMode = Literal["primary", "subagent", "all"]
type AgentPromptSource = Literal["builtin", "custom_markdown"]
type AgentPromptFormat = Literal["text", "markdown"]
type AgentSourceScope = Literal["builtin", "package", "project", "user"]


@dataclass(frozen=True, slots=True)
class AgentMcpBindingIntent:
    """Declarative MCP binding intent for an agent preset.

    This is deliberately not an execution authority. Agent declarations can ask
    runtime to bind configured MCP profiles/servers, but runtime remains the
    source of truth for server lifecycle, approval, and tool allowlist checks.
    """

    profile: str | None = None
    servers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.profile is not None and not self.profile.strip():
            raise ValueError("AgentMcpBindingIntent.profile must be a non-empty string")
        if len(self.servers) != len(set(self.servers)):
            raise ValueError("AgentMcpBindingIntent.servers must not contain duplicates")
        for server_name in self.servers:
            if not server_name.strip():
                raise ValueError("AgentMcpBindingIntent.servers entries must be non-empty strings")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.profile is not None:
            payload["profile"] = self.profile
        if self.servers:
            payload["servers"] = list(self.servers)
        return payload


@dataclass(frozen=True, slots=True)
class AgentPromptMaterialization:
    """Stable, audit-friendly description of how a manifest's prompt is rendered.

    The fields are intentionally narrow today: every builtin manifest renders a
    static text profile owned by `src/voidcode/agent/<profile>/base.txt`. The
    `version` integer is bumped whenever the prompt text or its materialization
    semantics change so external consumers can compare prompts deterministically.
    """

    profile: str
    version: int = 1
    source: AgentPromptSource = "builtin"
    format: AgentPromptFormat = "text"
    body: str | None = None
    prompt_append: str | None = None
    source_scope: AgentSourceScope | None = None
    source_path: str | None = None
    source_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("AgentPromptMaterialization.profile must be a non-empty string")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("AgentPromptMaterialization.version must be an integer >= 1")
        if (
            not isinstance(self.source, str)
            or self.source not in {"builtin", "custom_markdown"}
            or not isinstance(self.format, str)
            or self.format not in {"text", "markdown"}
        ):
            raise ValueError("invalid prompt source or format")
        if self.source == "custom_markdown":
            if not isinstance(self.body, str) or not self.body.strip():
                raise ValueError("AgentPromptMaterialization.body must be non-empty for custom_markdown")
            if self.format != "markdown":
                raise ValueError("AgentPromptMaterialization.format must be markdown for custom_markdown")
        if self.source == "builtin" and (self.format != "text" or self.body is not None or self.prompt_append is not None):
            raise ValueError("builtin prompt materialization must reference a text profile")
        for field_name in ("body", "prompt_append", "source_path", "source_id"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"AgentPromptMaterialization.{field_name} must be a non-empty string")
        if self.source_scope is not None and (
            not isinstance(self.source_scope, str) or self.source_scope not in {"builtin", "package", "project", "user"}
        ):
            raise ValueError("AgentPromptMaterialization.source_scope is invalid")
        if self.source_scope == "package" and self.source_id is None:
            raise ValueError("package prompt materialization requires source_id")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AgentPromptMaterialization:
        required = {"profile", "version", "source", "format"}
        allowed = required | {"body", "prompt_append", "source_scope", "source_path", "source_id"}
        if set(payload) - allowed or required - set(payload):
            raise ValueError("invalid current prompt materialization fields")
        return cls(
            profile=cast(str, payload["profile"]),
            version=cast(int, payload["version"]),
            source=cast(AgentPromptSource, payload["source"]),
            format=cast(AgentPromptFormat, payload["format"]),
            body=cast(str | None, payload.get("body")),
            prompt_append=cast(str | None, payload.get("prompt_append")),
            source_scope=cast(AgentSourceScope | None, payload.get("source_scope")),
            source_path=cast(str | None, payload.get("source_path")),
            source_id=cast(str | None, payload.get("source_id")),
        )

    def to_payload(self, *, profile: str | None = None) -> dict[str, object]:
        if profile is not None and (not isinstance(profile, str) or not profile.strip()):
            raise ValueError("prompt profile override must be non-empty")
        payload: dict[str, object] = {
            "profile": self.profile if profile is None else profile,
            "version": self.version,
            "source": self.source,
            "format": self.format,
        }
        if self.body is not None:
            payload["body"] = self.body
        if self.prompt_append is not None:
            payload["prompt_append"] = self.prompt_append
        if self.source_scope is not None:
            payload["source_scope"] = self.source_scope
        if self.source_path is not None:
            payload["source_path"] = self.source_path
        if self.source_id is not None:
            payload["source_id"] = self.source_id
        return payload


@dataclass(frozen=True, slots=True)
class AgentManifest:
    id: AgentManifestKey
    name: str
    mode: AgentMode
    description: str
    source_scope: AgentSourceScope = "builtin"
    source_path: str | None = None
    source_id: str | None = None
    prompt_profile: str | None = None
    model_preference: str | None = None
    fallback_models: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    #: Child read-only stance. Builtins declare it; customs have no stance field so they parse
    #: as False (spawn still ORs the parent lease, so a read-only parent keeps a custom child
    #: read-only). Never inferred from tool names.
    readonly: bool = False
    mcp_binding: AgentMcpBindingIntent | None = None
    top_level_selectable: bool = False
    prompt_materialization: AgentPromptMaterialization | None = None

    def __post_init__(self) -> None:
        for field_name in ("id", "name", "description"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"agent manifest {field_name} must be non-empty")
        if self.mode not in ("primary", "subagent", "all"):
            raise ValueError("invalid agent manifest mode")
        if self.source_scope not in ("builtin", "package", "project", "user"):
            raise ValueError("invalid agent manifest source_scope")
        if self.source_id is not None and (not isinstance(self.source_id, str) or not self.source_id.strip()):
            raise ValueError("agent manifest source_id must be non-empty")
        if self.source_scope == "package" and self.source_id is None:
            raise ValueError("package agent manifest requires source_id")
        for field_name in ("source_path", "prompt_profile", "model_preference"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"agent manifest {field_name} must be non-empty")
        for field_name in ("fallback_models", "tool_allowlist", "skill_refs"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"agent manifest {field_name} must be a tuple of non-empty strings")
        if type(self.top_level_selectable) is not bool:
            raise ValueError("agent top_level_selectable must be a boolean")
        if type(self.readonly) is not bool:
            raise ValueError("agent readonly must be a boolean")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "description": self.description,
            "source_scope": self.source_scope,
            "top_level_selectable": self.top_level_selectable,
        }
        if self.source_path is not None:
            payload["source_path"] = self.source_path
        if self.source_id is not None:
            payload["source_id"] = self.source_id
        if self.prompt_profile is not None:
            payload["prompt_profile"] = self.prompt_profile
        if self.model_preference is not None:
            payload["model_preference"] = self.model_preference
        if self.fallback_models:
            payload["fallback_models"] = list(self.fallback_models)
        if self.tool_allowlist:
            payload["tool_allowlist"] = list(self.tool_allowlist)
        if self.skill_refs:
            payload["skill_refs"] = list(self.skill_refs)
        if self.readonly:
            payload["readonly"] = True
        if self.mcp_binding is not None:
            payload["mcp_binding"] = self.mcp_binding.to_payload()
        if self.prompt_materialization is not None:
            payload["prompt_materialization"] = self.prompt_materialization.to_payload()
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AgentManifest:
        if not isinstance(payload, Mapping):
            raise ValueError("AgentManifest payload must be a mapping")
        for key in ("id", "name", "mode", "description"):
            if key not in payload or not isinstance(payload[key], str):
                raise ValueError(f"AgentManifest payload '{key}' must be a string")
        raw_selectable = payload.get("top_level_selectable", False)
        if type(raw_selectable) is not bool:
            raise ValueError("AgentManifest payload 'top_level_selectable' must be a boolean")
        raw_readonly = payload.get("readonly", False)
        if type(raw_readonly) is not bool:
            raise ValueError("AgentManifest payload 'readonly' must be a boolean")
        mcp_payload = payload.get("mcp_binding")
        mcp_binding = None
        if mcp_payload is not None:
            if not isinstance(mcp_payload, Mapping):
                raise ValueError("AgentManifest payload 'mcp_binding' must be an object")
            raw_servers = mcp_payload.get("servers", ())
            if not isinstance(raw_servers, (list, tuple)):
                raise ValueError("AgentManifest payload 'mcp_binding.servers' must be a sequence")
            mcp_binding = AgentMcpBindingIntent(
                profile=cast(str | None, mcp_payload.get("profile")),
                servers=tuple(cast(list[str], raw_servers)),
            )
        mat_payload = payload.get("prompt_materialization")
        prompt_materialization = None
        if mat_payload is not None:
            if not isinstance(mat_payload, Mapping):
                raise ValueError("AgentManifest payload 'prompt_materialization' must be an object")
            prompt_materialization = AgentPromptMaterialization.from_payload(mat_payload)

        def _parse_tuple(field_name: str) -> tuple[str, ...]:
            raw = payload.get(field_name, ())
            if not isinstance(raw, (list, tuple)):
                raise ValueError(f"AgentManifest payload '{field_name}' must be a sequence of strings")
            return tuple(cast(list[str], raw))

        def _parse_opt_str(field_name: str) -> str | None:
            val = payload.get(field_name)
            if val is not None and not isinstance(val, str):
                raise ValueError(f"AgentManifest payload '{field_name}' must be a string")
            return val

        return cls(
            id=cast(str, payload["id"]),
            name=cast(str, payload["name"]),
            mode=cast(AgentMode, payload["mode"]),
            description=cast(str, payload["description"]),
            source_scope=cast(AgentSourceScope, payload.get("source_scope", "builtin")),
            source_path=_parse_opt_str("source_path"),
            source_id=_parse_opt_str("source_id"),
            prompt_profile=_parse_opt_str("prompt_profile"),
            model_preference=_parse_opt_str("model_preference"),
            fallback_models=_parse_tuple("fallback_models"),
            tool_allowlist=_parse_tuple("tool_allowlist"),
            skill_refs=_parse_tuple("skill_refs"),
            mcp_binding=mcp_binding,
            top_level_selectable=raw_selectable,
            readonly=raw_readonly,
            prompt_materialization=prompt_materialization,
        )
