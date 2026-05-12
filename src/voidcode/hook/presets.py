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
type HookPresetEventScope = Literal[
    "runtime.request_received",
    "runtime.hook_presets_loaded",
    "graph.model_turn",
    "graph.tool_request_created",
    "runtime.permission_resolved",
    "runtime.tool_started",
    "runtime.tool_completed",
    "runtime.background_task_registered",
    "runtime.background_task_started",
    "runtime.background_task_completed",
    "runtime.background_task_failed",
    "runtime.background_task_cancelled",
    "runtime.background_task_result_read",
    "runtime.delegated_result_available",
    "runtime.todo_updated",
    "runtime.turn_progress",
    "runtime.stuck_detected",
]
type HookPresetAction = Literal["observe", "report", "cancel", "guidance"]

_VALID_HOOK_PRESET_EVENT_SCOPES: tuple[HookPresetEventScope, ...] = (
    "runtime.request_received",
    "runtime.hook_presets_loaded",
    "graph.model_turn",
    "graph.tool_request_created",
    "runtime.permission_resolved",
    "runtime.tool_started",
    "runtime.tool_completed",
    "runtime.background_task_registered",
    "runtime.background_task_started",
    "runtime.background_task_completed",
    "runtime.background_task_failed",
    "runtime.background_task_cancelled",
    "runtime.background_task_result_read",
    "runtime.delegated_result_available",
    "runtime.todo_updated",
    "runtime.turn_progress",
    "runtime.stuck_detected",
)
_VALID_HOOK_PRESET_ACTIONS: tuple[HookPresetAction, ...] = (
    "observe",
    "report",
    "cancel",
    "guidance",
)
_FORBIDDEN_HOOK_PRESET_ACTIONS: frozenset[str] = frozenset(
    {
        "grant_tools",
        "widen_tool_defaults",
        "enable_product_delegation",
        "create_child_task",
        "bypass_approval",
        "mutate_policy_truth",
        "rewrite_tool_args",
    }
)


def validate_hook_preset_event_scopes(
    scopes: tuple[str, ...],
    *,
    field_path: str,
) -> tuple[HookPresetEventScope, ...]:
    valid_scopes = frozenset(_VALID_HOOK_PRESET_EVENT_SCOPES)
    parsed: list[HookPresetEventScope] = []
    if not scopes:
        raise ValueError(f"{field_path} must contain at least one event scope")
    for scope in scopes:
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")
        if scope not in valid_scopes:
            allowed = ", ".join(_VALID_HOOK_PRESET_EVENT_SCOPES)
            raise ValueError(
                f"{field_path} contains invalid event scope: {scope}; valid scopes are: {allowed}"
            )
        parsed.append(cast(HookPresetEventScope, scope))
    return tuple(parsed)


def validate_hook_preset_actions(
    actions: tuple[str, ...],
    *,
    field_path: str,
) -> tuple[HookPresetAction, ...]:
    valid_actions = frozenset(_VALID_HOOK_PRESET_ACTIONS)
    parsed: list[HookPresetAction] = []
    if not actions:
        raise ValueError(f"{field_path} must contain at least one action")
    for action in actions:
        if not isinstance(action, str) or not action.strip():
            raise ValueError(f"{field_path} entries must be non-empty strings")
        if action in _FORBIDDEN_HOOK_PRESET_ACTIONS:
            raise ValueError(f"{field_path} contains forbidden authority action: {action}")
        if action not in valid_actions:
            allowed = ", ".join(_VALID_HOOK_PRESET_ACTIONS)
            raise ValueError(
                f"{field_path} contains invalid action: {action}; valid actions are: {allowed}"
            )
        parsed.append(cast(HookPresetAction, action))
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class HookPreset:
    ref: HookPresetRef
    kind: HookPresetKind
    description: str
    guidance: str
    event_scopes: tuple[HookPresetEventScope, ...]
    allowed_actions: tuple[HookPresetAction, ...] = ("guidance",)

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("HookPreset.ref must be a non-empty string")
        if not self.description.strip():
            raise ValueError("HookPreset.description must be a non-empty string")
        if not self.guidance.strip():
            raise ValueError("HookPreset.guidance must be a non-empty string")
        validate_hook_preset_event_scopes(
            self.event_scopes,
            field_path=f"hook preset '{self.ref}' event_scopes",
        )
        validate_hook_preset_actions(
            self.allowed_actions,
            field_path=f"hook preset '{self.ref}' allowed_actions",
        )


@dataclass(frozen=True, slots=True)
class ResolvedHookPresetSnapshot:
    refs: tuple[str, ...]
    presets: tuple[dict[str, object], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "refs": list(self.refs),
            "presets": [_hook_preset_payload(preset) for preset in self.presets],
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
            event_scopes = ", ".join(cast(tuple[str, ...], preset["event_scopes"]))
            allowed_actions = ", ".join(cast(tuple[str, ...], preset["allowed_actions"]))
            lines.extend(
                (
                    "  <hook_preset>",
                    f"    <ref>{preset['ref']}</ref>",
                    f"    <kind>{preset['kind']}</kind>",
                    f"    <source>{preset['source']}</source>",
                    f"    <event_scopes>{event_scopes}</event_scopes>",
                    f"    <allowed_actions>{allowed_actions}</allowed_actions>",
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
            event_scopes=("runtime.request_received", "graph.model_turn"),
            allowed_actions=("guidance",),
        ),
        "delegation_guard": HookPreset(
            ref="delegation_guard",
            kind="guard",
            description="Keep delegated work inside runtime-owned routing and preset limits.",
            guidance=(
                "Delegate only through runtime-owned task routing, respect supported child "
                "presets, and never bypass runtime tool, approval, or session governance."
            ),
            event_scopes=(
                "graph.tool_request_created",
                "runtime.permission_resolved",
                "runtime.tool_started",
            ),
            allowed_actions=("observe", "report", "cancel", "guidance"),
        ),
        "background_output_quality_guidance": HookPreset(
            ref="background_output_quality_guidance",
            kind="guidance",
            description="Encourage bounded, useful background task result retrieval.",
            guidance=(
                "When reading background task or process output, request only the detail needed "
                "for the current decision, do not poll immediately after starting background "
                "work unless you need a real status check, prefer waiting for the runtime "
                "completion reminder or a meaningful state change, reuse returned task/process "
                "ids instead of starting duplicates, and summarize results before acting on them."
            ),
            event_scopes=(
                "runtime.background_task_completed",
                "runtime.background_task_failed",
                "runtime.background_task_cancelled",
                "runtime.background_task_result_read",
                "runtime.delegated_result_available",
            ),
            allowed_actions=("observe", "report", "guidance"),
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
            event_scopes=(
                "runtime.background_task_failed",
                "runtime.background_task_cancelled",
                "runtime.delegated_result_available",
            ),
            allowed_actions=("observe", "report", "guidance"),
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
            event_scopes=(
                "runtime.background_task_registered",
                "runtime.background_task_started",
                "runtime.background_task_completed",
                "runtime.background_task_failed",
                "runtime.background_task_cancelled",
                "runtime.delegated_result_available",
            ),
            allowed_actions=("observe", "report", "guidance"),
        ),
        "todo_continuation_guidance": HookPreset(
            ref="todo_continuation_guidance",
            kind="continuation",
            description="Keep multi-step work tracked and continued through explicit todos.",
            guidance=(
                "For multi-step work, keep todos current, complete finished items immediately, "
                "and use remaining todos to resume the next concrete action."
            ),
            event_scopes=(
                "runtime.todo_updated",
                "runtime.turn_progress",
                "runtime.stuck_detected",
            ),
            allowed_actions=("observe", "report", "guidance"),
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
    presets: list[dict[str, object]] = []
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
                "event_scopes": preset.event_scopes,
                "allowed_actions": preset.allowed_actions,
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
    presets: list[dict[str, object]] = []
    for index, raw_preset in enumerate(cast(list[object], raw_presets)):
        if not isinstance(raw_preset, dict):
            raise ValueError(f"persisted hook preset snapshot presets[{index}] must be an object")
        preset_payload = cast(dict[object, object], raw_preset)
        ref = preset_payload.get("ref")
        kind = preset_payload.get("kind")
        source = preset_payload.get("source")
        raw_event_scopes = preset_payload.get("event_scopes")
        raw_allowed_actions = preset_payload.get("allowed_actions")
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
        if not isinstance(raw_event_scopes, list):
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].event_scopes must be an array"
            )
        if not all(isinstance(item, str) for item in raw_event_scopes):
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].event_scopes entries "
                "must be strings"
            )
        if not isinstance(raw_allowed_actions, list):
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].allowed_actions must be an array"
            )
        if not all(isinstance(item, str) for item in raw_allowed_actions):
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].allowed_actions entries "
                "must be strings"
            )
        event_scopes = validate_hook_preset_event_scopes(
            tuple(cast(list[str], raw_event_scopes)),
            field_path=f"persisted hook preset snapshot presets[{index}].event_scopes",
        )
        allowed_actions = validate_hook_preset_actions(
            tuple(cast(list[str], raw_allowed_actions)),
            field_path=f"persisted hook preset snapshot presets[{index}].allowed_actions",
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
        if event_scopes != builtin.event_scopes:
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].event_scopes do not match "
                f"builtin hook preset: {ref}"
            )
        if allowed_actions != builtin.allowed_actions:
            raise ValueError(
                f"persisted hook preset snapshot presets[{index}].allowed_actions do not match "
                f"builtin hook preset: {ref}"
            )
        refs.append(ref)
        presets.append(
            {
                "ref": ref,
                "kind": kind,
                "source": source,
                "event_scopes": event_scopes,
                "allowed_actions": allowed_actions,
                "guidance": guidance,
            }
        )
    return ResolvedHookPresetSnapshot(refs=tuple(refs), presets=tuple(presets))


def _hook_preset_payload(preset: Mapping[str, object]) -> dict[str, object]:
    payload = dict(preset)
    payload["event_scopes"] = list(cast(tuple[str, ...], preset["event_scopes"]))
    payload["allowed_actions"] = list(cast(tuple[str, ...], preset["allowed_actions"]))
    return payload
