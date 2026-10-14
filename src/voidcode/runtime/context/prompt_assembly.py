from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, TypeIs

from voidcode.agent.prompt_sections import dynamic_boundary_marker
from voidcode.common.output_limits import PROMPT_FRAGMENT_PREVIEW_CHARS as _PROMPT_FRAGMENT_PREVIEW_CHARS
from voidcode.common.output_limits import truncate

from .transforms import RuntimeContextTransformResult

_BASE_SAFETY_GUIDANCE = "Follow runtime safety policies. Runtime enforcement is authoritative over prompt text."


_TOOL_POLICY_SUMMARY = "Tools: visible list is advisory. Runtime allowlists and policy control execution."


# ponytail: fixed caps (8 items x 2000 chars); combine into one section or size by token budget if pressure matters.
_MAX_HOOK_GUIDANCE_ITEMS = 8
_MAX_HOOK_GUIDANCE_CHARS = 2000
_HOOK_GUIDANCE_TRUNCATION_MARKER = "…[truncated]"
_HOOK_GUIDANCE_SOURCE = "hook_argv_guidance"


def _bounded_hook_guidance_items(guidance: Iterable[str] | None) -> tuple[tuple[str, dict[str, object]], ...]:
    """Normalize already-parsed argv hook guidance into bounded (content, provenance) pairs.

    Fail-open: any unexpected shape returns no items so the prompt is unchanged.
    Provenance carries sha256/keys only, never raw bodies.
    """
    try:
        if guidance is None:
            return ()
        if isinstance(guidance, str):
            candidates: list[object] = [guidance]
        else:
            candidates = list(guidance)
    except TypeError:
        return ()
    items: list[tuple[str, dict[str, object]]] = []
    seen: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        normalized = raw.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if len(normalized) > _MAX_HOOK_GUIDANCE_CHARS:
            normalized = f"{normalized[:_MAX_HOOK_GUIDANCE_CHARS]}{_HOOK_GUIDANCE_TRUNCATION_MARKER}"
            truncated = True
        else:
            truncated = False
        items.append(
            (
                normalized,
                {
                    "source": _HOOK_GUIDANCE_SOURCE,
                    "guidance_sha256": digest,
                    "guidance_index": len(items),
                    "truncated": truncated,
                },
            )
        )
        if len(items) >= _MAX_HOOK_GUIDANCE_ITEMS:
            break
    return tuple(items)


type ContextTier = Literal["instruction", "workspace", "task", "recent"]

_CONTEXT_TIERS: Final[frozenset[ContextTier]] = frozenset({"instruction", "workspace", "task", "recent"})


def is_context_tier(value: object) -> TypeIs[ContextTier]:
    """Whether an untrusted ``tier`` value names one of the four context tiers."""
    return value in _CONTEXT_TIERS


@dataclass(frozen=True, slots=True)
class PromptAssemblySection:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    source: str
    tier: ContextTier
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _PromptContextBlock:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    source: str
    tier: ContextTier
    metadata: Mapping[str, object] = field(default_factory=dict)
    layer: str | None = None

    def to_section(self) -> PromptAssemblySection:
        return PromptAssemblySection(
            role=self.role,
            content=self.content,
            source=self.source,
            tier=self.tier,
            metadata=_section_metadata(self.metadata, layer=self.layer),
        )


@dataclass(frozen=True, slots=True)
class PromptAssemblyFragment:
    id: str
    role: Literal["system", "user", "assistant", "tool"]
    source: str
    layer: str
    order: int
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
            "fragment_count": len(self.fragments),
            "fragments": [fragment.metadata_payload() for fragment in self.fragments],
        }


def build_prompt_assembly_plan(
    *,
    prompt: str,
    runtime_instruction_precedence: str,
    agent_prompt_context: str = "",
    skill_prompt_context: str = "",
    context_transform_result: RuntimeContextTransformResult | None = None,
    pending_state_section: PromptAssemblySection | None = None,
    todo_prompt_context: str = "",
    continuity_summary: str = "",
    artifact_reference_sections: Iterable[PromptAssemblySection] = (),
    tool_catalog_context: str = "",
    hook_guidance: Iterable[str] | None = None,
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
        block = _normalize_prompt_context_block(
            content,
            source=source,
            tier=tier,
            layer=layer,
            metadata=metadata,
        )
        if block is None or block.content in seen_system_contents:
            return
        seen_system_contents.add(block.content)
        sections.append(block.to_section())

    def append_block(
        content: str,
        *,
        source: str,
        tier: Literal["instruction", "workspace", "task", "recent"],
        layer: str | None,
        metadata: Mapping[str, object] | None = None,
        role: Literal["system", "user", "assistant", "tool"] = "system",
        strip_content: bool = True,
        allow_empty: bool = False,
    ) -> None:
        block = _normalize_prompt_context_block(
            content,
            source=source,
            tier=tier,
            layer=layer,
            metadata=metadata,
            role=role,
            strip_content=strip_content,
            allow_empty=allow_empty,
        )
        if block is None:
            return
        sections.append(block.to_section())

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
        layer="agent_instructions",
    )
    append_system(
        skill_prompt_context,
        source="skill_prompt",
        tier="instruction",
        layer="skills",
    )

    append_system(
        _TOOL_POLICY_SUMMARY,
        source="runtime_tool_policy_summary",
        tier="instruction",
        layer="tool_policy_summary",
    )
    append_system(
        tool_catalog_context,
        source="runtime_tool_catalog",
        tier="instruction",
        layer="tool_catalog",
    )
    append_system(
        dynamic_boundary_marker(),
        source="runtime_dynamic_boundary",
        tier="workspace",
        layer="project_context",
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
                    source=metadata_source(injection.metadata, fallback="context_transform"),
                    tier=_metadata_tier(injection.metadata, fallback="workspace"),
                    layer="hook_injected_context",
                    metadata=injection.metadata,
                )
                continue
            append_block(
                normalized,
                role=injection.role,
                source=metadata_source(injection.metadata, fallback="context_transform"),
                tier=_metadata_tier(injection.metadata, fallback="workspace"),
                layer="hook_injected_context",
                metadata=injection.metadata,
            )
    for guidance_content, guidance_metadata in _bounded_hook_guidance_items(hook_guidance):
        append_system(
            guidance_content,
            source=_HOOK_GUIDANCE_SOURCE,
            tier="workspace",
            layer="hook_injected_context",
            metadata=guidance_metadata,
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
            append_block(
                pending_state_section.content,
                role=pending_state_section.role,
                source=pending_state_section.source,
                tier=pending_state_section.tier,
                layer=None,
                metadata=pending_state_section.metadata,
            )

    append_system(
        todo_prompt_context,
        source="runtime_todo_state",
        tier="task",
        layer="task_state",
    )
    append_system(
        continuity_summary,
        source="context_projection",
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
        append_block(
            artifact_reference.content,
            role=artifact_reference.role,
            source=artifact_reference.source,
            tier=artifact_reference.tier,
            layer=None,
            metadata=artifact_reference.metadata,
        )

    append_block(
        prompt,
        role="user",
        source="current_user_prompt",
        tier="task",
        layer="user_request",
        metadata={
            "source": "current_user_prompt",
            "tier": "task",
        },
        strip_content=False,
        allow_empty=True,
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
        preview, truncated = _bounded_preview(section.content)
        fragments.append(
            PromptAssemblyFragment(
                id=f"{order:03d}:{section.source}",
                role=section.role,
                source=section.source,
                layer=layer,
                order=order,
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


def _normalize_prompt_context_block(
    content: str,
    *,
    source: str,
    tier: Literal["instruction", "workspace", "task", "recent"],
    layer: str | None,
    metadata: Mapping[str, object] | None,
    role: Literal["system", "user", "assistant", "tool"] = "system",
    strip_content: bool = True,
    allow_empty: bool = False,
) -> _PromptContextBlock | None:
    normalized_content = content.strip() if strip_content else content
    if not normalized_content and not allow_empty:
        return None
    return _PromptContextBlock(
        role=role,
        content=normalized_content,
        source=source,
        tier=tier,
        metadata={} if metadata is None else dict(metadata),
        layer=layer,
    )


def _metadata_layer(metadata: Mapping[str, object], *, fallback: str) -> str:
    layer = metadata.get("layer")
    return layer if isinstance(layer, str) and layer.strip() else fallback


def _default_layer_for_source(source: str) -> str:
    if source in {"runtime_base_safety", "runtime_instruction_precedence"}:
        return "base_safety"
    if source == "agent_prompt":
        return "agent_instructions"
    if source == "skill_prompt":
        return "skills"
    if source in {"context_transform", "hook_preset_guidance", "hook_argv_guidance"}:
        return "hook_injected_context"
    if source == "runtime_tool_policy_summary":
        return "tool_policy_summary"
    if source == "current_user_prompt":
        return "user_request"
    if source in {"runtime_todo_state", "runtime_pending_state"}:
        return "task_state"
    return "project_context"


def _bounded_preview(content: str) -> tuple[str, bool]:
    normalized = " ".join(content.split())
    if len(normalized) <= _PROMPT_FRAGMENT_PREVIEW_CHARS:
        return normalized, False
    return truncate(normalized, _PROMPT_FRAGMENT_PREVIEW_CHARS), True


def metadata_source(metadata: Mapping[str, object], *, fallback: str) -> str:
    """The ``source`` one assembled segment's metadata carries, else ``fallback``."""
    source = metadata.get("source")
    return source if isinstance(source, str) and source.strip() else fallback


def _metadata_tier(
    metadata: Mapping[str, object],
    *,
    fallback: ContextTier,
) -> ContextTier:
    tier = metadata.get("tier")
    return tier if is_context_tier(tier) else fallback


__all__ = [
    "ContextTier",
    "PromptAssemblyFragment",
    "PromptAssemblyPlan",
    "PromptAssemblySection",
    "build_prompt_assembly_plan",
    "is_context_tier",
    "metadata_source",
    "prompt_fragments_for_sections",
]
