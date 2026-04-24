from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal, cast

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


@dataclass(frozen=True, slots=True)
class SkillExecutionSnapshot:
    selected_skill_names: tuple[str, ...]
    applied_skill_payloads: tuple[dict[str, str], ...]
    skill_prompt_context: str
    snapshot_hash: str
    snapshot_version: int = 1
    source: Literal["run", "resume", "replay", "legacy"] = "run"
    binding_snapshot: dict[str, object] | None = None


_WHITESPACE_PATTERN = re.compile(r"[ \t]+")
_REQUIRED_SKILL_PAYLOAD_FIELDS = ("name", "description", "content")


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
    missing_fields = [
        field_name for field_name in _REQUIRED_SKILL_PAYLOAD_FIELDS if field_name not in payload
    ]
    if missing_fields:
        raise ValueError(
            "persisted skill payload missing required fields: " + ", ".join(missing_fields)
        )
    name = payload["name"].strip()
    description = payload["description"].strip()
    content = payload["content"].strip()
    if not name:
        raise ValueError("persisted skill payload field 'name' must be a non-empty string")
    if not description:
        raise ValueError("persisted skill payload field 'description' must be a non-empty string")
    if not content:
        raise ValueError("persisted skill payload field 'content' must be a non-empty string")
    prompt_context = payload.get("prompt_context")
    execution_notes = payload.get("execution_notes", content).strip()
    if not execution_notes:
        execution_notes = content
    if prompt_context is None:
        prompt_parts = [f"Skill: {name}"]
        if description:
            prompt_parts.append(f"Description: {description}")
        if execution_notes:
            prompt_parts.append(f"Instructions:\n{execution_notes}")
        prompt_context = "\n".join(prompt_parts).strip()
    else:
        prompt_context = prompt_context.strip()
        if not prompt_context:
            raise ValueError("persisted skill payload field 'prompt_context' must not be empty")
    return SkillRuntimeContext(
        name=name,
        description=description,
        content=content,
        prompt_context=prompt_context,
        execution_notes=execution_notes,
        source_path=payload.get("source_path", "").strip(),
    )


def _snapshot_payload_without_hash(snapshot: SkillExecutionSnapshot) -> dict[str, object]:
    return {
        "snapshot_version": snapshot.snapshot_version,
        "source": snapshot.source,
        "selected_skill_names": list(snapshot.selected_skill_names),
        "applied_skill_payloads": [dict(payload) for payload in snapshot.applied_skill_payloads],
        "skill_prompt_context": snapshot.skill_prompt_context,
        "binding_snapshot": snapshot.binding_snapshot,
    }


def _snapshot_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_skill_execution_snapshot(
    contexts: Iterable[SkillRuntimeContext],
    *,
    source: Literal["run", "resume", "replay", "legacy"] = "run",
    selected_skill_names: Iterable[str] | None = None,
    binding_snapshot: dict[str, object] | None = None,
) -> SkillExecutionSnapshot:
    context_values = tuple(contexts)
    frozen_payloads = tuple(
        {
            "name": context.name,
            "description": context.description,
            "content": context.content,
            "prompt_context": context.prompt_context,
            "execution_notes": context.execution_notes,
            "source_path": context.source_path,
        }
        for context in context_values
    )
    selected_names = (
        tuple(selected_skill_names)
        if selected_skill_names is not None
        else tuple(context.name for context in context_values)
    )
    snapshot_without_hash: dict[str, object] = {
        "snapshot_version": 1,
        "source": source,
        "selected_skill_names": list(selected_names),
        "applied_skill_payloads": [dict(payload) for payload in frozen_payloads],
        "skill_prompt_context": build_skill_prompt_context(context_values),
        "binding_snapshot": binding_snapshot,
    }
    return SkillExecutionSnapshot(
        selected_skill_names=selected_names,
        applied_skill_payloads=frozen_payloads,
        skill_prompt_context=cast(str, snapshot_without_hash["skill_prompt_context"]),
        snapshot_hash=_snapshot_hash(snapshot_without_hash),
        snapshot_version=1,
        source=source,
        binding_snapshot=binding_snapshot,
    )


def with_skill_snapshot_bindings(
    snapshot: SkillExecutionSnapshot,
    *,
    binding_snapshot: dict[str, object] | None,
) -> SkillExecutionSnapshot:
    updated = replace(snapshot, binding_snapshot=binding_snapshot)
    return replace(updated, snapshot_hash=_snapshot_hash(_snapshot_payload_without_hash(updated)))


def snapshot_payload(snapshot: SkillExecutionSnapshot) -> dict[str, object]:
    payload = _snapshot_payload_without_hash(snapshot)
    payload["snapshot_hash"] = snapshot.snapshot_hash
    return payload


def snapshot_from_payload(payload: dict[str, object]) -> SkillExecutionSnapshot:
    raw_selected = payload.get("selected_skill_names", [])
    if not isinstance(raw_selected, list):
        raise ValueError("persisted skill snapshot selected_skill_names must be a list[str]")
    raw_selected_items = cast(list[object], raw_selected)
    if not all(isinstance(item, str) for item in raw_selected_items):
        raise ValueError("persisted skill snapshot selected_skill_names must be a list[str]")
    selected_skill_names = cast(list[str], raw_selected_items)
    raw_applied = payload.get("applied_skill_payloads", [])
    if not isinstance(raw_applied, list):
        raise ValueError("persisted skill snapshot applied_skill_payloads must be a list")
    raw_applied_items = cast(list[object], raw_applied)
    applied_payloads: list[dict[str, str]] = []
    for item in raw_applied_items:
        if not isinstance(item, dict):
            raise ValueError("persisted skill snapshot payload entries must be objects")
        normalized: dict[str, str] = {}
        for key, value in cast(dict[str, object], item).items():
            if not isinstance(value, str):
                raise ValueError("persisted skill snapshot payload values must be strings")
            normalized[key] = value
        _ = runtime_context_from_payload(normalized)
        applied_payloads.append(normalized)
    source = payload.get("source", "legacy")
    if source not in {"run", "resume", "replay", "legacy"}:
        source = "legacy"
    snapshot_version = payload.get("snapshot_version", 1)
    if not isinstance(snapshot_version, int):
        raise ValueError("persisted skill snapshot version must be an integer")
    skill_prompt_context = payload.get("skill_prompt_context", "")
    if not isinstance(skill_prompt_context, str):
        raise ValueError("persisted skill snapshot skill_prompt_context must be a string")
    binding_snapshot = payload.get("binding_snapshot")
    if binding_snapshot is not None and not isinstance(binding_snapshot, dict):
        raise ValueError("persisted skill snapshot binding_snapshot must be an object")
    snapshot_without_hash: dict[str, object] = {
        "snapshot_version": snapshot_version,
        "source": source,
        "selected_skill_names": selected_skill_names,
        "applied_skill_payloads": applied_payloads,
        "skill_prompt_context": skill_prompt_context,
        "binding_snapshot": binding_snapshot,
    }
    computed_hash = _snapshot_hash(snapshot_without_hash)
    return SkillExecutionSnapshot(
        selected_skill_names=tuple(selected_skill_names),
        applied_skill_payloads=tuple(applied_payloads),
        skill_prompt_context=skill_prompt_context,
        snapshot_hash=computed_hash,
        snapshot_version=snapshot_version,
        source=cast(Literal["run", "resume", "replay", "legacy"], source),
        binding_snapshot=cast(dict[str, object] | None, binding_snapshot),
    )


__all__ = [
    "SkillRuntimeContext",
    "build_runtime_context",
    "build_runtime_contexts",
    "build_skill_execution_snapshot",
    "build_skill_prompt_context",
    "runtime_context_from_payload",
    "SkillExecutionSnapshot",
    "snapshot_from_payload",
    "snapshot_payload",
    "with_skill_snapshot_bindings",
]
