from __future__ import annotations

import platform
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from voidcode.agent.profile_overlays import get_profile_overlay
from voidcode.agent.prompt_sections import (
    capability_block,
    dynamic_boundary_marker,
    identity_header,
    prompt_activation_guidance_block,
)

from .context_transforms import RuntimeContextTransformResult

_PROMPT_FRAGMENT_PREVIEW_CHARS = 240
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(access[_-]?token\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(client[_-]?secret\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(password\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(secret\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(token\s*[=:]\s*)[^\s,;]+"),
)

_TIER_PRIORITY = {
    "instruction": 100,
    "workspace": 200,
    "task": 300,
    "recent": 400,
}

_BASE_SAFETY_GUIDANCE = (
    "Base safety: follow runtime-enforced permission, approval, memory, shell, and "
    "tool policies. Prompt text describes policy intent; runtime controls remain the "
    "source of enforcement truth."
)

_STRICT_MEMORY_USAGE_GUIDANCE = (
    "Memory usage guidance: treat workspace memory as optional, bounded context. "
    "Prefer current repository files and live runtime state over remembered facts, do "
    "not store or repeat secrets, credentials, raw tokens, private keys, or sensitive "
    "environment values, and use memory tools only when runtime policy explicitly "
    "makes them available for this turn."
)

_TOOL_POLICY_SUMMARY = (
    "Tool policy summary: the visible tool list is advisory context for the model; "
    "runtime allowlists, read-only policy, shell classification, approvals, hooks, and "
    "tool lookup checks decide whether a call may execute."
)
_PROMPT_ACTIVATION_PREVIEW_CHARS = 160


@dataclass(frozen=True, slots=True)
class PromptActivationDecision:
    section: PromptAssemblySection | None
    metadata: dict[str, object]


def prompt_activation_decision(
    *,
    session_metadata: Mapping[str, object],
    prompt_profile_name: str | None,
) -> PromptActivationDecision:
    runtime_policy = _mapping_value(session_metadata.get("runtime_policy"))
    prompt_activation = _mapping_value(runtime_policy.get("prompt_activation"))
    enabled = prompt_activation.get("enabled", True) is not False
    mode = (
        _metadata_string(session_metadata, "mode")
        or _metadata_string(runtime_policy, "mode")
        or "normal"
    )
    intent = _mapping_value(runtime_policy.get("intent"))
    intent_slot = _metadata_string(intent, "label") or "unspecified"
    activation_id = _activation_id(prompt_profile_name)
    activation_key = _activation_key(
        activation_id=activation_id,
        mode=mode,
        intent_slot=intent_slot,
    )
    existing_records = _activation_records(prompt_activation.get("activated"))
    already_active = any(record.get("key") == activation_key for record in existing_records)
    profile_refs = _string_list(prompt_activation.get("profile_refs"))
    base_metadata: dict[str, object] = {
        **prompt_activation,
        "enabled": enabled,
        "activation_id": activation_id,
        "mode": mode,
        "intent_slot": intent_slot,
        "granularity": "session+activation_id+mode+intent_slot",
        "raw_prompt_stored": False,
        "activated": existing_records,
    }
    if not enabled or already_active:
        base_metadata["activated_this_turn"] = (
            session_metadata.get("_prompt_activation_this_run") is True
        )
        return PromptActivationDecision(section=None, metadata=base_metadata)

    guidance = prompt_activation_guidance_block(
        activation_id=activation_id,
        mode=mode,
        intent_slot=intent_slot,
        profile_refs=profile_refs,
    )
    preview_source = (
        f"Activation {activation_id} for mode {mode} and intent slot {intent_slot}. "
        "Guidance-only; runtime policy remains enforcement truth."
    )
    preview, preview_truncated = _redacted_preview(preview_source)
    record: dict[str, object] = {
        "key": activation_key,
        "activation_id": activation_id,
        "mode": mode,
        "intent_slot": intent_slot,
        "source": "runtime_policy",
        "guidance_only": True,
        "raw_prompt_stored": False,
        "preview": preview[:_PROMPT_ACTIVATION_PREVIEW_CHARS],
        "preview_truncated": preview_truncated or len(preview) > _PROMPT_ACTIVATION_PREVIEW_CHARS,
    }
    base_metadata["activated"] = [*existing_records, record]
    base_metadata["activated_this_turn"] = True
    base_metadata["last_activation"] = record
    return PromptActivationDecision(
        section=PromptAssemblySection(
            role="system",
            content=guidance,
            source="runtime_prompt_activation",
            tier="instruction",
            metadata={
                "layer": "prompt_activation",
                "activation_id": activation_id,
                "mode": mode,
                "intent_slot": intent_slot,
                "guidance_only": True,
            },
        ),
        metadata=base_metadata,
    )


def _activation_id(prompt_profile_name: str | None) -> str:
    profile = prompt_profile_name.strip() if prompt_profile_name is not None else ""
    return f"agent_prompt:{profile}" if profile else "agent_prompt:default"


def _activation_key(*, activation_id: str, mode: str, intent_slot: str) -> str:
    return f"{activation_id}|mode:{mode}|intent:{intent_slot}"


def _mapping_value(value: object) -> dict[str, object]:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _metadata_string(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _activation_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            record = dict(cast(Mapping[str, object], item))
            if isinstance(record.get("key"), str):
                records.append(record)
    return records


def build_env_card_sections(session_runtime_state: object) -> tuple[str, str]:
    workspace_root = _state_string(
        session_runtime_state,
        (
            "workspace_root",
            "workspace",
            "workspace_path",
            "cwd",
            "working_directory",
            "root_path",
        ),
    )
    model_identity = _model_identity(session_runtime_state)
    stable_lines = [
        "<environment_stable>",
        f"Platform: {platform.system()}",
        f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"Model: {model_identity}",
    ]
    if workspace_root is not None:
        stable_lines.append(f"Workspace root: {workspace_root}")
    stable_lines.append("</environment_stable>")

    dynamic_lines = [
        "<environment_dynamic>",
        f"Date: {datetime.now(UTC).date().isoformat()}",
    ]
    git_state = _git_dynamic_state(workspace_root)
    if git_state[0] is not None:
        dynamic_lines.append(f"Current branch: {git_state[0]}")
    if git_state[1] is not None:
        dynamic_lines.append(f"Git status: {git_state[1]}")
    dynamic_lines.append("</environment_dynamic>")
    return "\n".join(stable_lines), "\n".join(dynamic_lines)


def _model_identity(session_runtime_state: object) -> str:
    direct = _state_string(
        session_runtime_state,
        ("model_identity", "model_id", "model_name", "model"),
    )
    if direct is not None:
        return direct

    resolved_provider = _state_value(session_runtime_state, "resolved_provider")
    active_target = _state_value(resolved_provider, "active_target")
    selection = _state_value(active_target, "selection")
    selected = _state_string(selection, ("raw_model", "model"))
    if selected is not None:
        return selected

    effective_config = _state_value(session_runtime_state, "effective_config")
    configured = _state_string(effective_config, ("model",))
    return configured if configured is not None else "unknown"


def _git_dynamic_state(workspace_root: str | None) -> tuple[str | None, str | None]:
    if workspace_root is None:
        return None, None
    root = Path(workspace_root).expanduser()
    if not root.exists():
        return None, None
    branch = _run_git(root, ("rev-parse", "--abbrev-ref", "HEAD"))
    status_output = _run_git(root, ("status", "--short"), allow_empty=True)
    status_summary = _status_summary(status_output) if status_output is not None else None
    return branch, status_summary


def _run_git(
    workspace_root: Path, args: tuple[str, ...], *, allow_empty: bool = False
) -> str | None:
    try:
        result = subprocess.run(
            ("git", "-C", str(workspace_root), *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=0.2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if output or allow_empty:
        return output
    return None


def _status_summary(status_output: str | None) -> str | None:
    if status_output is None:
        return "clean"
    changed = len([line for line in status_output.splitlines() if line.strip()])
    if changed == 0:
        return "clean"
    return f"{changed} changed file{'s' if changed != 1 else ''}"


def _state_string(state: object, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _state_value(state, key)
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _state_value(state: object, key: str) -> object | None:
    if state is None:
        return None
    if isinstance(state, Mapping):
        value = cast(Mapping[str, object], state).get(key)
    else:
        value = getattr(state, key, None)
    if value is not None:
        return value
    metadata = _metadata_mapping(state)
    if metadata is None:
        return None
    value = metadata.get(key)
    if value is not None:
        return value
    runtime_state = metadata.get("runtime_state")
    if isinstance(runtime_state, Mapping):
        return cast(Mapping[str, Any], runtime_state).get(key)
    return None


def _metadata_mapping(state: object) -> Mapping[str, object] | None:
    if isinstance(state, Mapping):
        metadata = cast(Mapping[str, object], state).get("metadata")
    else:
        metadata = getattr(state, "metadata", None)
    if isinstance(metadata, Mapping):
        return cast(Mapping[str, object], metadata)
    return None


@dataclass(frozen=True, slots=True)
class PromptAssemblySection:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    source: str
    tier: Literal["instruction", "workspace", "task", "recent"]
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PromptAssemblyFragment:
    id: str
    role: Literal["system", "user", "assistant", "tool"]
    source: str
    layer: str
    order: int
    priority: int
    tier: Literal["instruction", "workspace", "task", "recent"]
    preview: str
    preview_truncated: bool
    content_chars: int

    def metadata_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "source": self.source,
            "layer": self.layer,
            "order": self.order,
            "priority": self.priority,
            "tier": self.tier,
            "preview": self.preview,
            "preview_truncated": self.preview_truncated,
            "content_chars": self.content_chars,
        }


@dataclass(frozen=True, slots=True)
class PromptAssemblyPlan:
    sections: tuple[PromptAssemblySection, ...] = ()
    fragments: tuple[PromptAssemblyFragment, ...] = ()

    def fragment_metadata_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "preview_chars": _PROMPT_FRAGMENT_PREVIEW_CHARS,
            "redacted": True,
            "fragment_count": len(self.fragments),
            "fragments": [fragment.metadata_payload() for fragment in self.fragments],
        }


def build_prompt_assembly_plan(
    *,
    prompt: str,
    runtime_instruction_precedence: str,
    agent_prompt_context: str = "",
    workflow_mode_prompt_context: str = "",
    preserved_system_segments: Iterable[str] = (),
    skill_prompt_context: str = "",
    context_transform_result: RuntimeContextTransformResult | None = None,
    pending_state_section: PromptAssemblySection | None = None,
    todo_prompt_context: str = "",
    workspace_memory_context: str = "",
    continuity_summary: str = "",
    artifact_reference_sections: Iterable[PromptAssemblySection] = (),
    prompt_profile_name: str | None = None,
    session_runtime_state: object | None = None,
    prompt_activation_section: PromptAssemblySection | None = None,
) -> PromptAssemblyPlan:
    sections: list[PromptAssemblySection] = []
    seen_system_contents: set[str] = set()

    def append_system(
        content: str,
        *,
        source: str,
        tier: Literal["instruction", "workspace", "task", "recent"],
        layer: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        normalized = content.strip()
        if not normalized or normalized in seen_system_contents:
            return
        seen_system_contents.add(normalized)
        sections.append(
            PromptAssemblySection(
                role="system",
                content=normalized,
                source=source,
                tier=tier,
                metadata=_section_metadata(metadata, layer=layer),
            )
        )

    profile_overlay = (
        get_profile_overlay(prompt_profile_name) if prompt_profile_name is not None else None
    )
    stable_env_card = ""
    dynamic_env_card = ""
    if session_runtime_state is not None:
        stable_env_card, dynamic_env_card = build_env_card_sections(session_runtime_state)

    if profile_overlay is not None:
        append_system(
            _BASE_SAFETY_GUIDANCE,
            source="runtime_base_safety",
            tier="instruction",
            layer="base_safety",
        )
        append_system(
            identity_header(profile_overlay.profile_name, profile_overlay.role_summary),
            source="agent_identity_header",
            tier="instruction",
            layer="persona_profile",
        )
        append_system(
            capability_block(list(profile_overlay.capabilities)),
            source="agent_capability_block",
            tier="instruction",
            layer="persona_profile",
        )
        append_system(
            agent_prompt_context,
            source="agent_prompt",
            tier="instruction",
            layer="persona_profile",
        )
        for prompt_section in profile_overlay.prompt_sections:
            append_system(
                prompt_section,
                source="agent_profile_overlay",
                tier="instruction",
                layer="persona_profile",
            )
        append_system(
            stable_env_card,
            source="runtime_environment_stable",
            tier="workspace",
            layer="project_context",
        )
        append_system(
            dynamic_boundary_marker(),
            source="runtime_dynamic_boundary",
            tier="workspace",
            layer="project_context",
        )
        append_system(
            dynamic_env_card,
            source="runtime_environment_dynamic",
            tier="workspace",
            layer="project_context",
        )
    else:
        append_system(
            _BASE_SAFETY_GUIDANCE,
            source="runtime_base_safety",
            tier="instruction",
            layer="base_safety",
        )
        append_system(
            runtime_instruction_precedence,
            source="runtime_instruction_precedence",
            tier="instruction",
            layer="base_safety",
        )
        append_system(
            agent_prompt_context,
            source="agent_prompt",
            tier="instruction",
            layer="persona_profile",
        )
    if profile_overlay is not None:
        append_system(
            runtime_instruction_precedence,
            source="runtime_instruction_precedence",
            tier="instruction",
            layer="base_safety",
        )
    append_system(
        workflow_mode_prompt_context,
        source="workflow_mode_prompt",
        tier="instruction",
        layer="mode_policy",
    )
    if prompt_activation_section is not None:
        append_system(
            prompt_activation_section.content,
            source=prompt_activation_section.source,
            tier=prompt_activation_section.tier,
            layer="prompt_activation",
            metadata=prompt_activation_section.metadata,
        )
    for segment_content in preserved_system_segments:
        append_system(
            segment_content,
            source="preserved_system_segment",
            tier="instruction",
            layer="base_safety",
        )
    append_system(
        _STRICT_MEMORY_USAGE_GUIDANCE,
        source="runtime_memory_usage_guidance",
        tier="instruction",
        layer="memory_usage_guidance",
    )
    append_system(
        skill_prompt_context,
        source="skill_prompt",
        tier="instruction",
        layer="skills",
    )

    transform_result = context_transform_result
    if transform_result is not None:
        for injection in transform_result.injections:
            normalized = injection.content.strip()
            if not normalized:
                continue
            if injection.role == "system":
                append_system(
                    normalized,
                    source=_metadata_source(injection.metadata, fallback="context_transform"),
                    tier=_metadata_tier(injection.metadata, fallback="workspace"),
                    layer="hook_injected_context",
                    metadata=injection.metadata,
                )
                continue
            sections.append(
                PromptAssemblySection(
                    role=cast(Literal["system", "user", "assistant", "tool"], injection.role),
                    content=normalized,
                    source=_metadata_source(injection.metadata, fallback="context_transform"),
                    tier=_metadata_tier(injection.metadata, fallback="workspace"),
                    metadata=_section_metadata(
                        injection.metadata,
                        layer="hook_injected_context",
                    ),
                )
            )

    if pending_state_section is not None:
        if pending_state_section.role == "system":
            append_system(
                pending_state_section.content,
                source=pending_state_section.source,
                tier=pending_state_section.tier,
                layer="task_state",
                metadata=pending_state_section.metadata,
            )
        else:
            sections.append(pending_state_section)

    append_system(
        todo_prompt_context,
        source="runtime_todo_state",
        tier="task",
        layer="task_state",
    )
    append_system(
        workspace_memory_context,
        source="runtime_workspace_memory",
        tier="workspace",
        layer="project_context",
        metadata={"section": "Workspace Memory"},
    )
    append_system(
        _TOOL_POLICY_SUMMARY,
        source="runtime_tool_policy_summary",
        tier="instruction",
        layer="tool_policy_summary",
    )
    append_system(
        continuity_summary,
        source="continuity_summary",
        tier="recent",
        layer="project_context",
    )

    for artifact_reference in artifact_reference_sections:
        if artifact_reference.role == "system":
            append_system(
                artifact_reference.content,
                source=artifact_reference.source,
                tier=artifact_reference.tier,
                layer="project_context",
                metadata=artifact_reference.metadata,
            )
            continue
        sections.append(artifact_reference)

    sections.append(
        PromptAssemblySection(
            role="user",
            content=prompt,
            source="current_user_prompt",
            tier="task",
            metadata={
                "source": "current_user_prompt",
                "tier": "task",
                "layer": "user_request",
            },
        )
    )
    ordered_sections = tuple(sections)
    return PromptAssemblyPlan(
        sections=ordered_sections,
        fragments=prompt_fragments_for_sections(ordered_sections),
    )


def prompt_fragments_for_sections(
    sections: tuple[PromptAssemblySection, ...],
) -> tuple[PromptAssemblyFragment, ...]:
    fragments: list[PromptAssemblyFragment] = []
    for order, section in enumerate(sections):
        layer = _metadata_layer(
            section.metadata,
            fallback=_default_layer_for_source(section.source),
        )
        preview, truncated = _redacted_preview(section.content)
        fragments.append(
            PromptAssemblyFragment(
                id=f"{order:03d}:{section.source}",
                role=section.role,
                source=section.source,
                layer=layer,
                order=order,
                priority=_TIER_PRIORITY[section.tier] + order,
                tier=section.tier,
                preview=preview,
                preview_truncated=truncated,
                content_chars=len(section.content),
            )
        )
    return tuple(fragments)


def _section_metadata(
    metadata: Mapping[str, object] | None,
    *,
    layer: str | None,
) -> dict[str, object]:
    section_metadata = {} if metadata is None else dict(metadata)
    if layer is not None and "layer" not in section_metadata:
        section_metadata["layer"] = layer
    return section_metadata


def _metadata_layer(metadata: Mapping[str, object], *, fallback: str) -> str:
    layer = metadata.get("layer")
    return layer if isinstance(layer, str) and layer.strip() else fallback


def _default_layer_for_source(source: str) -> str:
    if source in {"runtime_base_safety", "runtime_instruction_precedence"}:
        return "base_safety"
    if source == "workflow_mode_prompt":
        return "mode_policy"
    if source == "runtime_prompt_activation":
        return "prompt_activation"
    if source == "runtime_memory_usage_guidance":
        return "memory_usage_guidance"
    if source.startswith("agent_"):
        return "persona_profile"
    if source == "skill_prompt":
        return "skills"
    if source in {"context_transform", "hook_preset_guidance"}:
        return "hook_injected_context"
    if source == "runtime_tool_policy_summary":
        return "tool_policy_summary"
    if source == "current_user_prompt":
        return "user_request"
    if source in {"runtime_todo_state", "runtime_pending_state"}:
        return "task_state"
    return "project_context"


def _redacted_preview(content: str) -> tuple[str, bool]:
    redacted = content
    for pattern in _SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(r"\1[redacted]", redacted)
    redacted = " ".join(redacted.split())
    if len(redacted) <= _PROMPT_FRAGMENT_PREVIEW_CHARS:
        return redacted, False
    return f"{redacted[:_PROMPT_FRAGMENT_PREVIEW_CHARS]}...", True


def _metadata_source(metadata: Mapping[str, object], *, fallback: str) -> str:
    source = metadata.get("source")
    return source if isinstance(source, str) and source.strip() else fallback


def _metadata_tier(
    metadata: Mapping[str, object],
    *,
    fallback: Literal["instruction", "workspace", "task", "recent"],
) -> Literal["instruction", "workspace", "task", "recent"]:
    tier = metadata.get("tier")
    if tier in {"instruction", "workspace", "task", "recent"}:
        return cast(Literal["instruction", "workspace", "task", "recent"], tier)
    return fallback


__all__ = [
    "PromptAssemblyPlan",
    "PromptAssemblyFragment",
    "build_env_card_sections",
    "PromptAssemblySection",
    "build_prompt_assembly_plan",
    "prompt_activation_decision",
    "prompt_fragments_for_sections",
]
