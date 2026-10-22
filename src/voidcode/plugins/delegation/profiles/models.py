"""Agent manifest value records owned by the delegation plugin.

`AgentManifest` is the discovered profile record; `AgentPromptMaterialization`
describes how that profile's prompt is rendered.
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
type AgentMode = Literal["primary", "subagent"]
type AgentPromptSource = Literal["builtin", "custom_markdown"]
type AgentSourceScope = Literal["builtin", "package", "project", "user"]


@dataclass(frozen=True, slots=True)
class AgentPromptMaterialization:
    """How a manifest's prompt is rendered.

    Builtin profiles render their ``texts/<profile>.txt`` resource; custom markdown
    profiles render their parsed ``body`` plus the optional ``prompt_append``.
    """

    profile: str
    source: AgentPromptSource = "builtin"
    body: str | None = None
    prompt_append: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("AgentPromptMaterialization.profile must be a non-empty string")
        if not isinstance(self.source, str) or self.source not in {"builtin", "custom_markdown"}:
            raise ValueError("invalid prompt source")
        if self.source == "custom_markdown":
            if not isinstance(self.body, str) or not self.body.strip():
                raise ValueError("AgentPromptMaterialization.body must be non-empty for custom_markdown")
        if self.source == "builtin" and (self.body is not None or self.prompt_append is not None):
            raise ValueError("builtin prompt materialization must reference a text profile")
        for field_name in ("body", "prompt_append"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"AgentPromptMaterialization.{field_name} must be a non-empty string")

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> AgentPromptMaterialization:
        required = {"profile", "source"}
        allowed = required | {"body", "prompt_append"}
        if set(payload) - allowed or required - set(payload):
            raise ValueError("invalid current prompt materialization fields")
        return cls(
            profile=cast(str, payload["profile"]),
            source=cast(AgentPromptSource, payload["source"]),
            body=cast(str | None, payload.get("body")),
            prompt_append=cast(str | None, payload.get("prompt_append")),
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "profile": self.profile,
            "source": self.source,
        }
        if self.body is not None:
            payload["body"] = self.body
        if self.prompt_append is not None:
            payload["prompt_append"] = self.prompt_append
        return payload


@dataclass(frozen=True, slots=True)
class AgentManifest:
    id: AgentManifestKey
    name: str
    mode: AgentMode
    description: str
    source_scope: AgentSourceScope = "builtin"
    source_path: str | None = None
    prompt_profile: str | None = None
    model_preference: str | None = None
    fallback_models: tuple[str, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    #: Child read-only stance. Builtins declare it; customs have no stance field so they parse
    #: as False (spawn still ORs the parent lease, so a read-only parent keeps a custom child
    #: read-only). Never inferred from tool names.
    readonly: bool = False
    top_level_selectable: bool = False
    prompt_materialization: AgentPromptMaterialization | None = None

    def __post_init__(self) -> None:
        for field_name in ("id", "name", "description"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"agent manifest {field_name} must be non-empty")
        if self.mode not in ("primary", "subagent"):
            raise ValueError("invalid agent manifest mode")
        if self.source_scope not in ("builtin", "package", "project", "user"):
            raise ValueError("invalid agent manifest source_scope")
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
            prompt_profile=_parse_opt_str("prompt_profile"),
            model_preference=_parse_opt_str("model_preference"),
            fallback_models=_parse_tuple("fallback_models"),
            tool_allowlist=_parse_tuple("tool_allowlist"),
            skill_refs=_parse_tuple("skill_refs"),
            top_level_selectable=raw_selectable,
            readonly=raw_readonly,
            prompt_materialization=prompt_materialization,
        )
