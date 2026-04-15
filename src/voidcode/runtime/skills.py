from __future__ import annotations

from dataclasses import dataclass

from ..skills.registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class SkillRuntimeContext:
    name: str
    description: str
    content: str


def build_runtime_contexts(registry: SkillRegistry) -> tuple[SkillRuntimeContext, ...]:
    return tuple(
        SkillRuntimeContext(
            name=skill.name,
            description=skill.description,
            content=skill.content,
        )
        for skill in registry.all()
    )


__all__ = [
    "SkillRuntimeContext",
    "build_runtime_contexts",
]
