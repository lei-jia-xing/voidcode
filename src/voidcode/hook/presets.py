from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

type HookPresetRef = Literal[
    "role_reminder",
    "delegation_guard",
    "background_output_quality_guidance",
    "delegated_retry_guidance",
    "delegated_task_timing_guidance",
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


@dataclass(frozen=True, slots=True)
class ResolvedHookPresetSnapshot:
    refs: tuple[str, ...]
    presets: tuple[dict[str, str], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "refs": list(self.refs),
            "presets": [dict(preset) for preset in self.presets],
            "source": "builtin",
            "version": 1,
        }

    def guidance_context(self) -> str:
        if not self.presets:
            return ""
        lines = [
            "Resolved agent hook preset guidance.",
            (
                "These instructions are advisory runtime context only: they do not expand tool "
                "permissions, approval behavior, delegation budget, or lifecycle hook execution."
            ),
            "",
            "<hook_presets>",
        ]
        for preset in self.presets:
            lines.extend(
                (
                    "  <hook_preset>",
                    f"    <ref>{preset['ref']}</ref>",
                    f"    <kind>{preset['kind']}</kind>",
                    f"    <source>{preset['source']}</source>",
                    f"    <guidance>{preset['guidance']}</guidance>",
                    "  </hook_preset>",
                )
            )
        lines.append("</hook_presets>")
        return "\n".join(lines)


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
                "current decision, do not poll immediately after starting a background task unless "
                "you need a real status check, prefer waiting for the runtime completion reminder, "
                "and summarize results before acting on them."
            ),
        ),
        "delegated_retry_guidance": HookPreset(
            ref="delegated_retry_guidance",
            kind="guard",
            description="Keep delegated retry decisions explicit and leader-owned.",
            guidance=(
                "Retry failed, cancelled, or interrupted delegated background tasks only when it "
                "is the next explicit recovery step. Use the runtime-owned background_retry tool "
                "from a leader context instead of manually reconstructing child requests, inspect "
                "the new task id with background_output, and escalate repeated failures rather "
                "than looping."
            ),
        ),
        "delegated_task_timing_guidance": HookPreset(
            ref="delegated_task_timing_guidance",
            kind="guidance",
            description=(
                "Keep delegated background work asynchronous unless waiting is intentional."
            ),
            guidance=(
                "After starting delegated background work, continue other safe work first. "
                "Treat task ids as references, not immediate prompts to poll. "
                "Use foreground multi-tool calls for short independent reads/searches instead "
                "of spawning child work. "
                "Check status only when it changes the next decision, prefer waiting for "
                "runtime completion or failure reminders, and use blocking result reads "
                "only when you intentionally want to wait in the current turn."
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
    if ref == "delegated_retry_guidance":
        return "delegated_retry_guidance"
    if ref == "delegated_task_timing_guidance":
        return "delegated_task_timing_guidance"
    if ref == "todo_continuation_guidance":
        return "todo_continuation_guidance"
    return None


def validate_hook_preset_refs(
    refs: tuple[str, ...],
    *,
    field_path: str,
) -> tuple[str, ...]:
    valid_refs = ", ".join(preset.ref for preset in list_builtin_hook_presets())
    for ref in refs:
        if not ref.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")
        if not is_builtin_hook_preset_ref(ref):
            raise ValueError(
                f"{field_path} references unknown hook preset: {ref}; "
                f"valid presets are: {valid_refs}"
            )
    return refs


def resolve_hook_preset_refs(refs: tuple[str, ...]) -> ResolvedHookPresetSnapshot:
    _ = validate_hook_preset_refs(refs, field_path="hook preset refs")
    seen_refs: list[str] = []
    presets: list[dict[str, str]] = []
    for ref in refs:
        if ref in seen_refs:
            continue
        preset = get_builtin_hook_preset(ref)
        if preset is None:
            raise ValueError(f"hook preset refs references unknown hook preset: {ref}")
        seen_refs.append(ref)
        presets.append(
            {
                "ref": preset.ref,
                "kind": preset.kind,
                "source": "builtin",
                "guidance": preset.guidance,
            }
        )
    return ResolvedHookPresetSnapshot(refs=tuple(seen_refs), presets=tuple(presets))


def hook_preset_snapshot_from_payload(payload: object) -> ResolvedHookPresetSnapshot | None:
    if not isinstance(payload, dict):
        return None
    payload_items = cast(dict[object, object], payload)
    raw_presets = payload_items.get("presets")
    if not isinstance(raw_presets, list):
        return None
    refs: list[str] = []
    presets: list[dict[str, str]] = []
    for index, raw_preset in enumerate(cast(list[object], raw_presets)):
        if not isinstance(raw_preset, dict):
            raise ValueError(f"persisted hook preset snapshot presets[{index}] must be an object")
        preset_payload = cast(dict[object, object], raw_preset)
        ref = preset_payload.get("ref")
        kind = preset_payload.get("kind")
        source = preset_payload.get("source")
        guidance = preset_payload.get("guidance")
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].ref must be a string"
            )
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].kind must be a string"
            )
        if not isinstance(source, str) or not source.strip():
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].source must be a string"
            )
        if not isinstance(guidance, str) or not guidance.strip():
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].guidance must be a string"
            )
        builtin = get_builtin_hook_preset(ref)
        if builtin is None:
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].ref references unknown "
                f"hook preset: {ref}"
            )
        if source != "builtin":
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].source must be builtin"
            )
        if kind != builtin.kind:
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].kind does not match "
                f"builtin hook preset: {ref}"
            )
        if guidance != builtin.guidance:
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].guidance does not match "
                f"builtin hook preset: {ref}"
            )
        refs.append(ref)
        presets.append({"ref": ref, "kind": kind, "source": source, "guidance": guidance})
    return ResolvedHookPresetSnapshot(refs=tuple(refs), presets=tuple(presets))
