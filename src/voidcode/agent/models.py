from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

type AgentManifestId = Literal[
    "leader",
    "worker",
    "advisor",
    "explore",
    "researcher",
    "product",
]
type AgentMode = Literal["primary", "subagent", "all"]
type AgentExecutionEngineName = Literal["deterministic", "provider"]
type AgentManifestFieldSemantic = Literal["live_default", "intent"]
type AgentPromptSource = Literal["builtin"]
type AgentPromptFormat = Literal["text"]


_EMPTY_MODEL_FAMILY_OVERRIDES: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class AgentPromptMaterialization:
    """Stable, audit-friendly description of how a manifest's prompt is rendered.

    The fields are intentionally narrow today: every builtin manifest renders a
    static text profile owned by `src/voidcode/agent/<profile>/base.txt`. The
    `version` integer is bumped whenever the persona text or its materialization
    semantics change so external consumers can compare prompts deterministically.

    `model_family_overrides` declares an optional mapping from a model-family
    hint (for example `"opencode"` or `"anthropic"`) to a builtin prompt
    profile name. Runtime callers may pass a model-family hint into
    `select_prompt_profile_for_manifest` to pick a family-tuned profile while
    still falling back to the default profile when no override is declared.
    """

    profile: str
    version: int = 1
    source: AgentPromptSource = "builtin"
    format: AgentPromptFormat = "text"
    model_family_overrides: Mapping[str, str] = field(
        default_factory=lambda: _EMPTY_MODEL_FAMILY_OVERRIDES,
    )

    def __post_init__(self) -> None:
        if not self.profile.strip():
            raise ValueError("AgentPromptMaterialization.profile must be a non-empty string")
        if self.version < 1:
            raise ValueError("AgentPromptMaterialization.version must be >= 1")
        for family, override_profile in self.model_family_overrides.items():
            if not family.strip():
                raise ValueError(
                    "AgentPromptMaterialization.model_family_overrides keys "
                    "must be non-empty strings"
                )
            if not override_profile.strip():
                raise ValueError(
                    "AgentPromptMaterialization.model_family_overrides values "
                    "must be non-empty strings"
                )

    def select_profile(self, model_family: str | None = None) -> str:
        """Return the profile to materialize for the given model family hint.

        Falls back to the default `profile` when `model_family` is `None` or
        when no override is declared for that family.
        """

        if model_family is None:
            return self.profile
        normalized = model_family.strip()
        if not normalized:
            return self.profile
        return self.model_family_overrides.get(normalized, self.profile)

    def to_payload(self, *, profile: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "profile": self.profile if profile is None else profile,
            "version": self.version,
            "source": self.source,
            "format": self.format,
        }
        if self.model_family_overrides:
            payload["model_family_overrides"] = dict(self.model_family_overrides)
        return payload


@dataclass(frozen=True, slots=True)
class AgentManifest:
    id: AgentManifestId
    name: str
    mode: AgentMode
    description: str
    prompt_profile: str | None = None
    execution_engine: AgentExecutionEngineName | None = None
    model_preference: str | None = None
    tool_allowlist: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    routing_hints: dict[str, object] = field(default_factory=dict)
    top_level_selectable: bool = False
    prompt_materialization: AgentPromptMaterialization | None = None

    @property
    def live_default_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self.prompt_profile is not None:
            fields.append("prompt_profile")
        if self.execution_engine is not None:
            fields.append("execution_engine")
        if self.model_preference is not None:
            fields.append("model_preference")
        if self.tool_allowlist:
            fields.append("tool_allowlist")
        if self.skill_refs:
            fields.append("skill_refs")
        fields.append("top_level_selectable")
        if self.prompt_materialization is not None:
            fields.append("prompt_materialization")
        return tuple(fields)

    @property
    def intent_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        if self.routing_hints:
            fields.append("routing_hints")
        return tuple(fields)

    def field_semantic(self, field_name: str) -> AgentManifestFieldSemantic:
        if field_name in self.live_default_fields:
            return "live_default"
        if field_name in self.intent_fields:
            return "intent"
        raise ValueError(f"unknown or unset manifest field semantic: {field_name}")
