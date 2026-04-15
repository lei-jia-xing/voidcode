from __future__ import annotations

import importlib.util
from dataclasses import dataclass
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


@runtime_checkable
class PlanContributor(Protocol):
    def apply(self, context: PlanContext) -> object: ...


class NoopPlanContributor:
    def apply(self, context: PlanContext) -> PlanPatch:
        _ = context
        return PlanPatch()


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
