from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, cast

from ..agent.builtin import get_builtin_agent_manifest
from ..hook.presets import validate_hook_preset_refs
from ..skills.builtin import list_builtin_skills

type WorkflowPresetId = Literal["research", "implementation", "frontend", "review", "git"]
type WorkflowPresetKey = WorkflowPresetId | str

_WORKFLOW_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
_WORKFLOW_PRESET_FIELDS = frozenset(
    {
        "id",
        "default_agent",
        "category",
        "prompt_append",
        "skill_refs",
        "force_load_skills",
        "hook_preset_refs",
        "mcp_binding_intents",
        "tool_policy_ref",
        "permission_policy_ref",
        "read_only_default",
        "verification_guidance",
    }
)
_MCP_BINDING_INTENT_FIELDS = frozenset({"profile", "servers", "required"})


@dataclass(frozen=True, slots=True)
class WorkflowMcpBindingIntent:
    """Declarative MCP binding need for a workflow preset.

    Required bindings fail validation when the named MCP profile/server is not
    available. Optional bindings remain representable so later snapshot logic can
    record deterministic degraded execution without resolving that state here.
    """

    profile: str | None = None
    servers: tuple[str, ...] = ()
    required: bool = True

    def __post_init__(self) -> None:
        if self.profile is None and not self.servers:
            raise ValueError("WorkflowMcpBindingIntent must declare a profile or server refs")
        if self.profile is not None and not self.profile.strip():
            raise ValueError("WorkflowMcpBindingIntent.profile must be a non-empty string")
        _validate_string_tuple(
            self.servers,
            field_path="WorkflowMcpBindingIntent.servers",
            allow_empty=True,
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"required": self.required}
        if self.profile is not None:
            payload["profile"] = self.profile
        if self.servers:
            payload["servers"] = list(self.servers)
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPreset:
    id: WorkflowPresetKey
    default_agent: str
    category: str
    prompt_append: str | None = None
    skill_refs: tuple[str, ...] = ()
    force_load_skills: tuple[str, ...] = ()
    hook_preset_refs: tuple[str, ...] = ()
    mcp_binding_intents: tuple[WorkflowMcpBindingIntent, ...] = ()
    tool_policy_ref: str | None = None
    permission_policy_ref: str | None = None
    read_only_default: bool = False
    verification_guidance: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _WORKFLOW_ID_PATTERN.fullmatch(self.id):
            raise ValueError(
                f"WorkflowPreset.id value {self.id!r} must match {_WORKFLOW_ID_PATTERN.pattern!r}"
            )
        if not self.default_agent.strip():
            raise ValueError(f"workflow preset '{self.id}' must declare a default_agent")
        if not self.category.strip():
            raise ValueError(f"workflow preset '{self.id}' must declare a category")
        if self.prompt_append is not None and not self.prompt_append.strip():
            raise ValueError(f"workflow preset '{self.id}' prompt_append must be non-empty")
        _validate_string_tuple(
            self.skill_refs,
            field_path=f"workflow preset '{self.id}' skill_refs",
            allow_empty=True,
        )
        _validate_string_tuple(
            self.force_load_skills,
            field_path=f"workflow preset '{self.id}' force_load_skills",
            allow_empty=True,
        )
        _validate_string_tuple(
            self.hook_preset_refs,
            field_path=f"workflow preset '{self.id}' hook_preset_refs",
            allow_empty=True,
        )
        validate_hook_preset_refs(
            self.hook_preset_refs,
            field_path=f"workflow preset '{self.id}' hook_preset_refs",
        )
        if self.tool_policy_ref is not None and not self.tool_policy_ref.strip():
            raise ValueError(f"workflow preset '{self.id}' tool_policy_ref must be non-empty")
        if self.permission_policy_ref is not None and not self.permission_policy_ref.strip():
            raise ValueError(f"workflow preset '{self.id}' permission_policy_ref must be non-empty")
        if self.verification_guidance is not None and not self.verification_guidance.strip():
            raise ValueError(f"workflow preset '{self.id}' verification_guidance must be non-empty")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "default_agent": self.default_agent,
            "category": self.category,
            "read_only_default": self.read_only_default,
        }
        if self.prompt_append is not None:
            payload["prompt_append"] = self.prompt_append
        if self.skill_refs:
            payload["skill_refs"] = list(self.skill_refs)
        if self.force_load_skills:
            payload["force_load_skills"] = list(self.force_load_skills)
        if self.hook_preset_refs:
            payload["hook_preset_refs"] = list(self.hook_preset_refs)
        if self.mcp_binding_intents:
            payload["mcp_binding_intents"] = [
                binding.to_payload() for binding in self.mcp_binding_intents
            ]
        if self.tool_policy_ref is not None:
            payload["tool_policy_ref"] = self.tool_policy_ref
        if self.permission_policy_ref is not None:
            payload["permission_policy_ref"] = self.permission_policy_ref
        if self.verification_guidance is not None:
            payload["verification_guidance"] = self.verification_guidance
        return payload


@dataclass(frozen=True, slots=True)
class WorkflowPresetRegistry:
    presets: Mapping[str, WorkflowPreset] = field(
        default_factory=lambda: MappingProxyType(_BUILTIN_WORKFLOW_PRESETS)
    )

    def get(self, preset_id: str) -> WorkflowPreset | None:
        return self.presets.get(preset_id)

    def list_presets(self) -> tuple[WorkflowPreset, ...]:
        return tuple(self.presets.values())


def list_builtin_workflow_presets() -> tuple[WorkflowPreset, ...]:
    return tuple(_BUILTIN_WORKFLOW_PRESETS.values())


def get_builtin_workflow_preset(preset_id: str) -> WorkflowPreset | None:
    return _BUILTIN_WORKFLOW_PRESETS.get(preset_id)


def load_builtin_workflow_preset_registry() -> WorkflowPresetRegistry:
    return WorkflowPresetRegistry(presets=MappingProxyType(_BUILTIN_WORKFLOW_PRESETS))


def validate_workflow_presets(
    presets: Iterable[WorkflowPreset],
    *,
    available_skill_names: Iterable[str] = (),
    available_mcp_profiles: Iterable[str] = (),
    available_mcp_servers: Iterable[str] = (),
) -> tuple[WorkflowPreset, ...]:
    available_skills = frozenset(
        (*tuple(skill.name for skill in list_builtin_skills()), *tuple(available_skill_names))
    )
    available_profiles = frozenset(available_mcp_profiles)
    available_servers = frozenset(available_mcp_servers)
    seen_ids: set[str] = set()
    validated: list[WorkflowPreset] = []
    for preset in presets:
        if preset.id in seen_ids:
            raise ValueError(f"duplicate workflow preset id: {preset.id}")
        seen_ids.add(preset.id)
        if get_builtin_agent_manifest(preset.default_agent) is None:
            raise ValueError(
                f"workflow preset '{preset.id}' references unknown default_agent "
                f"'{preset.default_agent}'"
            )
        for skill_name in preset.force_load_skills:
            if skill_name not in available_skills:
                raise ValueError(
                    f"workflow preset '{preset.id}' force_load_skills references missing skill: "
                    f"{skill_name}"
                )
        for skill_name in preset.skill_refs:
            if skill_name not in available_skills:
                raise ValueError(
                    f"workflow preset '{preset.id}' skill_refs references missing skill: "
                    f"{skill_name}"
                )
        for binding in preset.mcp_binding_intents:
            _validate_mcp_binding_availability(
                preset,
                binding,
                available_profiles=available_profiles,
                available_servers=available_servers,
            )
        validated.append(preset)
    return tuple(validated)


def workflow_preset_from_payload(
    payload: Mapping[str, object],
    *,
    field_path: str,
) -> WorkflowPreset:
    unknown = sorted(key for key in payload if key not in _WORKFLOW_PRESET_FIELDS)
    if unknown:
        raise ValueError(f"{field_path} has unsupported key(s): {', '.join(unknown)}")
    if "id" not in payload:
        raise ValueError(f"{field_path}.id is required")
    preset_id = _required_string(payload, "id", field_path=field_path)
    return WorkflowPreset(
        id=preset_id,
        default_agent=_required_string(payload, "default_agent", field_path=field_path),
        category=_required_string(payload, "category", field_path=field_path),
        prompt_append=_optional_string(payload, "prompt_append", field_path=field_path),
        skill_refs=_string_list(payload.get("skill_refs"), field_path=f"{field_path}.skill_refs"),
        force_load_skills=_string_list(
            payload.get("force_load_skills"),
            field_path=f"{field_path}.force_load_skills",
        ),
        hook_preset_refs=_string_list(
            payload.get("hook_preset_refs"),
            field_path=f"{field_path}.hook_preset_refs",
        ),
        mcp_binding_intents=_mcp_binding_intent_list(
            payload.get("mcp_binding_intents"),
            field_path=f"{field_path}.mcp_binding_intents",
        ),
        tool_policy_ref=_optional_string(payload, "tool_policy_ref", field_path=field_path),
        permission_policy_ref=_optional_string(
            payload,
            "permission_policy_ref",
            field_path=field_path,
        ),
        read_only_default=_optional_bool(
            payload.get("read_only_default"),
            field_path=f"{field_path}.read_only_default",
            default=False,
        ),
        verification_guidance=_optional_string(
            payload,
            "verification_guidance",
            field_path=field_path,
        ),
    )


def workflow_presets_from_payload(
    raw_presets: object,
    *,
    field_path: str = "workflows",
    available_skill_names: Iterable[str] = (),
    available_mcp_profiles: Iterable[str] = (),
    available_mcp_servers: Iterable[str] = (),
) -> WorkflowPresetRegistry | None:
    if raw_presets is None:
        return None
    if not isinstance(raw_presets, dict):
        raise ValueError(f"{field_path} must be an object when provided")
    parsed: list[WorkflowPreset] = []
    for key, raw_preset in cast(dict[object, object], raw_presets).items():
        if not isinstance(key, str) or not _WORKFLOW_ID_PATTERN.fullmatch(key):
            raise ValueError(f"{field_path} keys must match {_WORKFLOW_ID_PATTERN.pattern!r}")
        if not isinstance(raw_preset, dict):
            raise ValueError(f"{field_path}.{key} must be an object")
        payload = cast(dict[str, object], raw_preset)
        preset = workflow_preset_from_payload(payload, field_path=f"{field_path}.{key}")
        if preset.id != key:
            raise ValueError(f"{field_path}.{key}.id must match workflow preset map key")
        parsed.append(preset)
    validated = validate_workflow_presets(
        parsed,
        available_skill_names=available_skill_names,
        available_mcp_profiles=available_mcp_profiles,
        available_mcp_servers=available_mcp_servers,
    )
    return WorkflowPresetRegistry(
        presets=MappingProxyType({preset.id: preset for preset in validated})
    )


def _validate_mcp_binding_availability(
    preset: WorkflowPreset,
    binding: WorkflowMcpBindingIntent,
    *,
    available_profiles: frozenset[str],
    available_servers: frozenset[str],
) -> None:
    if not binding.required:
        return
    if binding.profile is not None and binding.profile not in available_profiles:
        raise ValueError(
            f"workflow preset '{preset.id}' required mcp_binding_intents profile is missing: "
            f"{binding.profile}"
        )
    for server_name in binding.servers:
        if server_name not in available_servers:
            raise ValueError(
                f"workflow preset '{preset.id}' required mcp_binding_intents server is missing: "
                f"{server_name}"
            )


def _validate_string_tuple(values: tuple[str, ...], *, field_path: str, allow_empty: bool) -> None:
    if not allow_empty and not values:
        raise ValueError(f"{field_path} must contain at least one string")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_path} must not contain duplicates")
    for value in values:
        if not value.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")


def _required_string(payload: Mapping[str, object], key: str, *, field_path: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_path}.{key} must be a non-empty string")
    return value.strip()


def _optional_string(payload: Mapping[str, object], key: str, *, field_path: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_path}.{key} must be a non-empty string")
    return value.strip()


def _optional_bool(value: object, *, field_path: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_path} must be a boolean when provided")
    return value


def _string_list(value: object, *, field_path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_path} must be an array when provided")
    parsed: list[str] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_path}[{index}] must be a non-empty string")
        parsed.append(item.strip())
    if len(parsed) != len(set(parsed)):
        raise ValueError(f"{field_path} must not contain duplicates")
    return tuple(parsed)


def _mcp_binding_intent_list(
    value: object,
    *,
    field_path: str,
) -> tuple[WorkflowMcpBindingIntent, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_path} must be an array when provided")
    parsed: list[WorkflowMcpBindingIntent] = []
    for index, item in enumerate(cast(list[object], value)):
        item_path = f"{field_path}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_path} must be an object")
        payload = cast(dict[str, object], item)
        unknown = sorted(key for key in payload if key not in _MCP_BINDING_INTENT_FIELDS)
        if unknown:
            raise ValueError(f"{item_path} has unsupported key(s): {', '.join(unknown)}")
        profile = payload.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile.strip()):
            raise ValueError(f"{item_path}.profile must be a non-empty string")
        parsed.append(
            WorkflowMcpBindingIntent(
                profile=profile.strip() if isinstance(profile, str) else None,
                servers=_string_list(payload.get("servers"), field_path=f"{item_path}.servers"),
                required=_optional_bool(
                    payload.get("required"),
                    field_path=f"{item_path}.required",
                    default=True,
                ),
            )
        )
    return tuple(parsed)


_VALIDATED_BUILTIN_WORKFLOW_PRESETS = validate_workflow_presets(
    (
        WorkflowPreset(
            id="research",
            default_agent="researcher",
            category="research",
            prompt_append=(
                "Use public documentation and examples only; distinguish official sources "
                "from incidental commentary. Prefer Context7-style documentation lookup, "
                "websearch-style public research, and grep_app-style code search when those "
                "configured capabilities are available."
            ),
            hook_preset_refs=(
                "role_reminder",
                "delegated_task_timing_guidance",
                "background_output_quality_guidance",
            ),
            mcp_binding_intents=(
                WorkflowMcpBindingIntent(
                    servers=("context7", "websearch", "grep_app"),
                    required=False,
                ),
            ),
            read_only_default=True,
            verification_guidance="Cite the evidence source and summarize confidence limits.",
        ),
        WorkflowPreset(
            id="implementation",
            default_agent="leader",
            category="implementation",
            prompt_append="Implement the requested change through runtime tools and verify it.",
            hook_preset_refs=(
                "role_reminder",
                "delegated_task_timing_guidance",
                "todo_continuation_guidance",
            ),
            permission_policy_ref="runtime_default",
            verification_guidance="Run focused tests or checks that cover the changed behavior.",
        ),
        WorkflowPreset(
            id="frontend",
            default_agent="leader",
            category="frontend",
            prompt_append=(
                "Respect frontend project guidance, apply frontend-design implementation "
                "guidance, and use Playwright/browser verification when a configured browser "
                "capability is available."
            ),
            skill_refs=("frontend-design", "playwright"),
            hook_preset_refs=(
                "role_reminder",
                "delegated_task_timing_guidance",
                "todo_continuation_guidance",
            ),
            mcp_binding_intents=(
                WorkflowMcpBindingIntent(servers=("playwright",), required=False),
            ),
            permission_policy_ref="runtime_default",
            verification_guidance="Run targeted frontend type, lint, test, or build checks.",
        ),
        WorkflowPreset(
            id="review",
            default_agent="advisor",
            category="review",
            prompt_append=(
                "Review the requested scope without mutating the workspace. Apply "
                "review-work result-quality guidance, read-only analysis, and configured "
                "documentation or code search capabilities when useful."
            ),
            skill_refs=("review-work",),
            hook_preset_refs=("role_reminder",),
            mcp_binding_intents=(
                WorkflowMcpBindingIntent(
                    servers=("context7", "websearch", "grep_app"),
                    required=False,
                ),
            ),
            read_only_default=True,
            verification_guidance="Report findings with severity and concrete file references.",
        ),
        WorkflowPreset(
            id="git",
            default_agent="leader",
            category="git",
            prompt_append=(
                "Apply git-master-style safety guidance. Keep git operations narrow, "
                "auditable, and user-requested. Inspect status and diff before changing "
                "repository state. Preserve hooks, avoid widening approvals, and rely on "
                "generic runtime approval and tool boundaries."
            ),
            skill_refs=("git-master",),
            hook_preset_refs=("role_reminder",),
            permission_policy_ref="runtime_default",
            verification_guidance=(
                "Check git status before and after the requested operation, preserve hooks, "
                "and keep repository mutations behind explicit user intent plus runtime approval."
            ),
        ),
    )
)

_BUILTIN_WORKFLOW_PRESETS: dict[str, WorkflowPreset] = {
    preset.id: preset for preset in _VALIDATED_BUILTIN_WORKFLOW_PRESETS
}


__all__ = [
    "WorkflowMcpBindingIntent",
    "WorkflowPreset",
    "WorkflowPresetId",
    "WorkflowPresetKey",
    "WorkflowPresetRegistry",
    "get_builtin_workflow_preset",
    "list_builtin_workflow_presets",
    "load_builtin_workflow_preset_registry",
    "validate_workflow_presets",
    "workflow_preset_from_payload",
    "workflow_presets_from_payload",
]
