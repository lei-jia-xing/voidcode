from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from .context_transforms import RuntimeContextTransformResult


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
    preserved_system_segments: Iterable[str] = (),
    skill_prompt_context: str = "",
    context_transform_result: RuntimeContextTransformResult | None = None,
    pending_state_section: PromptAssemblySection | None = None,
    todo_prompt_context: str = "",
    continuity_summary: str = "",
    artifact_reference_sections: Iterable[PromptAssemblySection] = (),
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

    append_system(
        runtime_instruction_precedence,
        source="runtime_instruction_precedence",
        tier="instruction",
    )
    append_system(agent_prompt_context, source="agent_prompt", tier="instruction")
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
    "PromptAssemblySection",
    "build_prompt_assembly_plan",
]
