"""Builtin hook preset ref names, copied from ``voidcode.hook.presets``.

Agent manifests only ever *reference* hook presets: the preset table itself
(kind, event scopes, allowed actions, guidance text) is owned by the hooks
plugin. This module keeps the ref-name validation with the same messages as
``validate_hook_preset_refs`` so a profile with an unknown or empty
``preset_hook_refs`` entry fails before it can be selected.

Kept as a local copy because plugin modules may not import each other's
packages (the host only imports modules from inside a plugin's own root).
"""

from __future__ import annotations

#: Builtin hook preset refs in their declared order, as enumerated by
#: ``voidcode.hook.presets._parse_builtin_hook_preset_ref``.
BUILTIN_HOOK_PRESET_REFS: tuple[str, ...] = (
    "role_reminder",
    "delegation_guard",
    "background_output_quality_guidance",
    "delegated_retry_guidance",
    "delegated_task_timing_guidance",
    "todo_continuation_guidance",
)


def is_builtin_hook_preset_ref(ref: str) -> bool:
    return ref in BUILTIN_HOOK_PRESET_REFS


def validate_hook_preset_refs(
    refs: tuple[str, ...],
    *,
    field_path: str,
) -> tuple[str, ...]:
    valid_refs = ", ".join(BUILTIN_HOOK_PRESET_REFS)
    for ref in refs:
        if not ref.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")
        if not is_builtin_hook_preset_ref(ref):
            raise ValueError(f"{field_path} references unknown hook preset: {ref}; valid presets are: {valid_refs}")
    return refs


__all__ = [
    "BUILTIN_HOOK_PRESET_REFS",
    "is_builtin_hook_preset_ref",
    "validate_hook_preset_refs",
]
