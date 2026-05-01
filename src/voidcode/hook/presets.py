from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

type HookPresetRef = Literal[
    "role_reminder",
    "delegation_guard",
    "background_output_quality_guidance",
    "todo_continuation_guidance",
]

type HookPresetKind = Literal["guidance", "guard", "continuation"]


@dataclass(frozen=True, slots=True)
class HookPreset:
    ref: HookPresetRef
    kind: HookPresetKind
    description: str
    guidance: str

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("HookPreset.ref must be a non-empty string")
        if not self.description.strip():
            raise ValueError("HookPreset.description must be a non-empty string")
        if not self.guidance.strip():
            raise ValueError("HookPreset.guidance must be a non-empty string")


_BUILTIN_HOOK_PRESETS: Mapping[HookPresetRef, HookPreset] = MappingProxyType(
    {
        "role_reminder": HookPreset(
            ref="role_reminder",
            kind="guidance",
            description="Remind the active agent to follow its selected role boundary.",
            guidance=(
                "Follow the active agent preset exactly: preserve its responsibility boundary, "
                "tool scope, and output obligations for this run."
            ),
        ),
        "delegation_guard": HookPreset(
            ref="delegation_guard",
            kind="guard",
            description="Keep delegated work inside runtime-owned routing and preset limits.",
            guidance=(
                "Delegate only through runtime-owned task routing, respect supported child "
                "presets, and never bypass runtime tool, approval, or session governance."
            ),
        ),
        "background_output_quality_guidance": HookPreset(
            ref="background_output_quality_guidance",
            kind="guidance",
            description="Encourage bounded, useful background task result retrieval.",
            guidance=(
                "When reading background task output, request only the detail needed for the "
                "current decision and summarize results before acting on them."
            ),
        ),
        "todo_continuation_guidance": HookPreset(
            ref="todo_continuation_guidance",
            kind="continuation",
            description="Keep multi-step work tracked and continued through explicit todos.",
            guidance=(
                "For multi-step work, keep todos current, complete finished items immediately, "
                "and use remaining todos to resume the next concrete action."
            ),
        ),
    }
)


def get_builtin_hook_preset(ref: str) -> HookPreset | None:
    hook_ref = _parse_builtin_hook_preset_ref(ref)
    if hook_ref is None:
        return None
    return _BUILTIN_HOOK_PRESETS[hook_ref]


def list_builtin_hook_presets() -> tuple[HookPreset, ...]:
    return tuple(_BUILTIN_HOOK_PRESETS.values())


def is_builtin_hook_preset_ref(ref: str) -> bool:
    return _parse_builtin_hook_preset_ref(ref) is not None


def _parse_builtin_hook_preset_ref(ref: str) -> HookPresetRef | None:
    if ref == "role_reminder":
        return "role_reminder"
    if ref == "delegation_guard":
        return "delegation_guard"
    if ref == "background_output_quality_guidance":
        return "background_output_quality_guidance"
    if ref == "todo_continuation_guidance":
        return "todo_continuation_guidance"
    return None


def validate_hook_preset_refs(
    refs: tuple[str, ...],
    *,
    field_path: str,
) -> tuple[str, ...]:
    for ref in refs:
        if not ref.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")
        if not is_builtin_hook_preset_ref(ref):
            valid_refs = ", ".join(preset.ref for preset in list_builtin_hook_presets())
            raise ValueError(
                f"{field_path} references unknown hook preset: {ref}; "
                f"valid presets are: {valid_refs}"
            )
    return refs
