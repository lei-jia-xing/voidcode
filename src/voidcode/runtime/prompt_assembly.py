from __future__ import annotations

import platform
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
)

from .context_transforms import RuntimeContextTransformResult


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
class PromptAssemblyPlan:
    sections: tuple[PromptAssemblySection, ...] = ()


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
) -> PromptAssemblyPlan:
    sections: list[PromptAssemblySection] = []
    seen_system_contents: set[str] = set()

    def append_system(
        content: str,
        *,
        source: str,
        tier: Literal["instruction", "workspace", "task", "recent"],
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
                metadata={} if metadata is None else dict(metadata),
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
            identity_header(profile_overlay.profile_name, profile_overlay.role_summary),
            source="agent_identity_header",
            tier="instruction",
        )
        append_system(
            capability_block(list(profile_overlay.capabilities)),
            source="agent_capability_block",
            tier="instruction",
        )
        append_system(agent_prompt_context, source="agent_prompt", tier="instruction")
        for prompt_section in profile_overlay.prompt_sections:
            append_system(
                prompt_section,
                source="agent_profile_overlay",
                tier="instruction",
            )
        append_system(stable_env_card, source="runtime_environment_stable", tier="workspace")
        append_system(
            dynamic_boundary_marker(),
            source="runtime_dynamic_boundary",
            tier="workspace",
        )
        append_system(dynamic_env_card, source="runtime_environment_dynamic", tier="workspace")
    else:
        append_system(
            runtime_instruction_precedence,
            source="runtime_instruction_precedence",
            tier="instruction",
        )
        append_system(agent_prompt_context, source="agent_prompt", tier="instruction")
    if profile_overlay is not None:
        append_system(
            runtime_instruction_precedence,
            source="runtime_instruction_precedence",
            tier="instruction",
        )
    append_system(
        workflow_mode_prompt_context,
        source="workflow_mode_prompt",
        tier="instruction",
    )
    for segment_content in preserved_system_segments:
        append_system(
            segment_content,
            source="preserved_system_segment",
            tier="instruction",
        )
    append_system(skill_prompt_context, source="skill_prompt", tier="instruction")

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
                    metadata=injection.metadata,
                )
                continue
            sections.append(
                PromptAssemblySection(
                    role=cast(Literal["system", "user", "assistant", "tool"], injection.role),
                    content=normalized,
                    source=_metadata_source(injection.metadata, fallback="context_transform"),
                    tier=_metadata_tier(injection.metadata, fallback="workspace"),
                    metadata=dict(injection.metadata),
                )
            )

    if pending_state_section is not None:
        if pending_state_section.role == "system":
            append_system(
                pending_state_section.content,
                source=pending_state_section.source,
                tier=pending_state_section.tier,
                metadata=pending_state_section.metadata,
            )
        else:
            sections.append(pending_state_section)

    append_system(todo_prompt_context, source="runtime_todo_state", tier="task")
    append_system(
        workspace_memory_context,
        source="runtime_workspace_memory",
        tier="workspace",
        metadata={"section": "Workspace Memory"},
    )
    append_system(continuity_summary, source="continuity_summary", tier="recent")

    for artifact_reference in artifact_reference_sections:
        if artifact_reference.role == "system":
            append_system(
                artifact_reference.content,
                source=artifact_reference.source,
                tier=artifact_reference.tier,
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
            metadata={"source": "current_user_prompt", "tier": "task"},
        )
    )
    return PromptAssemblyPlan(sections=tuple(sections))


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
    "build_env_card_sections",
    "PromptAssemblySection",
    "build_prompt_assembly_plan",
]
