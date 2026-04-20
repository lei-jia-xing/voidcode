from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..skills.models import SkillMetadata
from ..skills.registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class SkillRuntimeContext:
    name: str
    description: str
    content: str
    prompt_context: str
    execution_notes: str = ""
    source_path: str = ""


_WHITESPACE_PATTERN = re.compile(r"[ \t]+")


def _normalize_text(value: str) -> str:
    normalized_lines = [_WHITESPACE_PATTERN.sub(" ", line.strip()) for line in value.splitlines()]
    return "\n".join(line for line in normalized_lines if line).strip()


def build_runtime_context(skill: SkillMetadata) -> SkillRuntimeContext:
    description = _normalize_text(skill.description)
    content = _normalize_text(skill.content)
    execution_notes = content
    prompt_parts = [f"Skill: {skill.name}"]
    if description:
        prompt_parts.append(f"Description: {description}")
    if execution_notes:
        prompt_parts.append(f"Instructions:\n{execution_notes}")
    prompt_context = "\n".join(prompt_parts).strip()

    return SkillRuntimeContext(
        name=skill.name,
        description=description,
        content=content,
        prompt_context=prompt_context,
        execution_notes=execution_notes,
        source_path=str(skill.entry_path),
    )


def build_runtime_contexts(
    registry: SkillRegistry,
    *,
    skill_names: Iterable[str] | None = None,
) -> tuple[SkillRuntimeContext, ...]:
    if skill_names is None:
        skills = registry.all()
    else:
        skills = tuple(registry.resolve(skill_name) for skill_name in skill_names)
    return tuple(build_runtime_context(skill) for skill in skills)


def build_skill_prompt_context(contexts: Iterable[SkillRuntimeContext]) -> str:
    rendered = [context.prompt_context for context in contexts if context.prompt_context]
    if not rendered:
        return ""
    return (
        "Runtime-managed skills are active for this turn. "
        "Apply these instructions in addition to the user's request.\n\n" + "\n\n".join(rendered)
    )


def runtime_context_from_payload(payload: dict[str, str]) -> SkillRuntimeContext:
    name = payload["name"]
    description = payload["description"]
    content = payload["content"]
    prompt_context = payload.get("prompt_context")
    execution_notes = payload.get("execution_notes", content)
    if prompt_context is None:
        prompt_parts = [f"Skill: {name}"]
        if description:
            prompt_parts.append(f"Description: {description}")
        if execution_notes:
            prompt_parts.append(f"Instructions:\n{execution_notes}")
        prompt_context = "\n".join(prompt_parts).strip()
    return SkillRuntimeContext(
        name=name,
        description=description,
        content=content,
        prompt_context=prompt_context,
        execution_notes=execution_notes,
        source_path=payload.get("source_path", ""),
    )


__all__ = [
    "SkillRuntimeContext",
    "build_runtime_context",
    "build_runtime_contexts",
    "build_skill_prompt_context",
    "runtime_context_from_payload",
]
