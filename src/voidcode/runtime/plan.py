from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from .config import RuntimePlanConfig


@dataclass(frozen=True, slots=True)
class PlanContext:
    prompt: str
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class PlanPatch:
    prompt: str | None = None
    metadata_updates: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class PlanStep:
    order: int
    title: str
    acceptance_criteria: tuple[str, ...] = ()
    execution_hints: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanArtifact:
    version: int = 1
    kind: str = "leader.plan_first"
    steps: tuple[PlanStep, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    execution_hints: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class PlanContributor(Protocol):
    def apply(self, context: PlanContext) -> object: ...


class NoopPlanContributor:
    def apply(self, context: PlanContext) -> PlanPatch:
        _ = context
        return PlanPatch()


def build_plan_artifact(
    *,
    prompt: str,
    metadata: dict[str, object],
    agent_preset: str | None,
) -> PlanArtifact:
    stripped_prompt = prompt.strip()
    request_metadata_keys = tuple(sorted(key for key in metadata if key != "plan_artifact"))
    return PlanArtifact(
        steps=(
            PlanStep(
                order=1,
                title=(
                    f"Complete the requested work: {stripped_prompt}"
                    if stripped_prompt
                    else "Complete the requested work"
                ),
                acceptance_criteria=(
                    "Address the active user request before finishing execution.",
                ),
                execution_hints=("Preserve runtime-owned validation and approval boundaries.",),
                metadata={
                    "agent_preset": agent_preset,
                    "request_metadata_keys": list(request_metadata_keys),
                },
            ),
        ),
        acceptance_criteria=(
            "Execution should stay aligned with the requested task.",
            "Runtime validations must pass before graph/tool execution begins.",
        ),
        execution_hints=(
            "Persist the structured plan artifact in session metadata for replay/resume truth.",
            "Keep the legacy PlanPatch compatibility path active after artifact creation.",
        ),
        metadata={
            "agent_preset": agent_preset,
            "prompt": prompt,
            "request_metadata_keys": list(request_metadata_keys),
        },
    )


def serialize_plan_artifact(artifact: PlanArtifact) -> dict[str, object]:
    validate_plan_artifact(artifact)
    return {
        "version": artifact.version,
        "kind": artifact.kind,
        "steps": [
            {
                "order": step.order,
                "title": step.title,
                "acceptance_criteria": list(step.acceptance_criteria),
                "execution_hints": list(step.execution_hints),
                "metadata": _normalize_json_mapping(step.metadata, field_path="plan step metadata"),
            }
            for step in artifact.steps
        ],
        "acceptance_criteria": list(artifact.acceptance_criteria),
        "execution_hints": list(artifact.execution_hints),
        "metadata": _normalize_json_mapping(artifact.metadata, field_path="plan artifact metadata"),
    }


def validate_plan_artifact(artifact: object) -> PlanArtifact:
    if not isinstance(artifact, PlanArtifact):
        raise ValueError("runtime plan artifact must be a PlanArtifact")
    if artifact.version != 1:
        raise ValueError("runtime plan artifact version must be 1")
    if artifact.kind != "leader.plan_first":
        raise ValueError("runtime plan artifact kind must be 'leader.plan_first'")
    if not artifact.steps:
        raise ValueError("runtime plan artifact must include at least one step")
    if not artifact.acceptance_criteria:
        raise ValueError("runtime plan artifact must include acceptance criteria")
    if not artifact.execution_hints:
        raise ValueError("runtime plan artifact must include execution hints")
    _normalize_json_mapping(artifact.metadata, field_path="plan artifact metadata")
    for index, step in enumerate(artifact.steps, start=1):
        if step.order != index:
            raise ValueError("runtime plan artifact steps must use contiguous order values")
        if not step.title.strip():
            raise ValueError(f"runtime plan artifact step {index} title must be non-empty")
        _normalize_string_tuple(
            step.acceptance_criteria,
            field_path=f"runtime plan artifact step {index} acceptance criteria",
        )
        _normalize_string_tuple(
            step.execution_hints,
            field_path=f"runtime plan artifact step {index} execution hints",
        )
        _normalize_json_mapping(
            step.metadata,
            field_path=f"runtime plan artifact step {index} metadata",
        )
    _normalize_string_tuple(
        artifact.acceptance_criteria,
        field_path="runtime plan artifact acceptance criteria",
    )
    _normalize_string_tuple(
        artifact.execution_hints,
        field_path="runtime plan artifact execution hints",
    )
    return artifact


def _normalize_string_tuple(values: object, *, field_path: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_path} must be a tuple of non-empty strings")
    normalized: list[str] = []
    for value in cast(tuple[object, ...], values):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_path} must contain only non-empty strings")
        normalized.append(value)
    return tuple(normalized)


def _normalize_json_mapping(mapping: object, *, field_path: str) -> dict[str, object]:
    if not isinstance(mapping, dict):
        raise ValueError(f"{field_path} must be a dict with string keys")
    normalized: dict[str, object] = {}
    for key, value in cast(dict[object, object], mapping).items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field_path} must use non-empty string keys")
        normalized[key] = _normalize_json_value(value, field_path=f"{field_path}.{key}")
    return normalized


def _normalize_json_value(value: object, *, field_path: str) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, tuple):
        tuple_items = cast(tuple[object, ...], value)
        return [_normalize_json_value(item, field_path=field_path) for item in tuple_items]
    if isinstance(value, list):
        list_items = cast(list[object], value)
        return [_normalize_json_value(item, field_path=field_path) for item in list_items]
    if isinstance(value, dict):
        dict_value = cast(dict[object, object], value)
        return _normalize_json_mapping(dict_value, field_path=field_path)
    raise ValueError(f"{field_path} must be JSON-serializable")


def build_plan_contributor(
    workspace: Path,
    config: RuntimePlanConfig | None,
) -> PlanContributor:
    if config is None:
        return NoopPlanContributor()

    provider = (config.provider or "builtin").strip().lower()
    if provider == "builtin":
        return NoopPlanContributor()
    if provider != "custom":
        raise ValueError("runtime config field 'plan.provider' must be 'builtin' or 'custom'")

    module_path_value = config.module
    if module_path_value is None:
        raise ValueError("runtime config field 'plan.module' is required when provider is custom")
    module_path = Path(module_path_value)
    if not module_path.is_absolute():
        module_path = workspace / module_path
    module_path = module_path.resolve()

    if not module_path.exists():
        raise ValueError(f"runtime plan module does not exist: {module_path}")

    spec = importlib.util.spec_from_file_location("voidcode_runtime_plan_extension", module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"failed to load runtime plan module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    factory_name = config.factory or "build"
    factory_obj = getattr(module, factory_name, None)
    if factory_obj is None or not callable(factory_obj):
        raise ValueError(
            f"runtime plan module '{module_path}' must export callable '{factory_name}'"
        )

    options = {} if config.options is None else dict(config.options)
    contributor_obj = factory_obj(options)
    if not isinstance(contributor_obj, PlanContributor):
        apply_fn = getattr(contributor_obj, "apply", None)
        if apply_fn is None or not callable(apply_fn):
            raise ValueError("runtime plan contributor must implement apply(context) -> PlanPatch")

    return cast(PlanContributor, contributor_obj)


def apply_plan_patch(
    *,
    contributor: PlanContributor,
    prompt: str,
    metadata: dict[str, object],
) -> tuple[str, dict[str, object]]:
    raw_patch = contributor.apply(PlanContext(prompt=prompt, metadata=dict(metadata)))
    if not isinstance(raw_patch, PlanPatch):
        raise ValueError("runtime plan contributor must return PlanPatch")
    patch = raw_patch

    next_prompt = prompt if patch.prompt is None else patch.prompt
    next_metadata = dict(metadata)
    if patch.metadata_updates is not None:
        next_metadata.update(patch.metadata_updates)
    return next_prompt, next_metadata
