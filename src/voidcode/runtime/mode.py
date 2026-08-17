from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

type RuntimeMode = Literal["normal", "plan"]


def parse_runtime_mode(value: object) -> RuntimeMode:
    if value == "normal":
        return "normal"
    if value == "plan":
        return "plan"
    raise ValueError("runtime mode must be 'normal' or 'plan'")


def runtime_mode_from_metadata(metadata: Mapping[str, object] | None) -> RuntimeMode:
    if metadata is None:
        return "normal"
    return parse_runtime_mode(metadata.get("mode", "normal"))


@dataclass(frozen=True, slots=True)
class ModeDefinition:
    """Declarative description of a runtime mode's orthogonal switches.

    A mode is a named combination of reusable switches, not an enum case with
    scattered consumers: adding a mode only adds one declaration here.
    """

    name: RuntimeMode
    description: str
    read_only: bool = False
    transform_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parse_runtime_mode(self.name)
        if not self.description.strip():
            raise ValueError(f"mode '{self.name}' must declare a description")
        if len(self.transform_refs) != len(set(self.transform_refs)):
            raise ValueError(f"mode '{self.name}' transform_refs must not contain duplicates")
        if any(not ref.strip() for ref in self.transform_refs):
            raise ValueError(f"mode '{self.name}' transform_refs entries must be non-empty strings")


MODE_DEFINITIONS: dict[RuntimeMode, ModeDefinition] = {
    "normal": ModeDefinition(
        name="normal",
        description="Balanced default execution stance.",
    ),
    "plan": ModeDefinition(
        name="plan",
        description="Plan mode is active: read-only stance; produce a plan before writing code.",
        read_only=True,
        transform_refs=("mode_guidance",),
    ),
}


@dataclass(frozen=True, slots=True)
class ModeResolution:
    mode: RuntimeMode
    read_only: bool
    transform_refs: tuple[str, ...] = ()
    source: Literal["command", "metadata", "inherited", "default"] = "default"


def resolve_mode(
    mode: RuntimeMode,
    *,
    explicit_read_only: bool = False,
    source: Literal["command", "metadata", "inherited", "default"] = "default",
) -> ModeResolution:
    """Single aggregation point for a request's effective mode behavior.

    Pure function over the persisted ``mode`` scalar plus any explicit
    ``read_only`` metadata. Consumers (permission layer, tool policy layer,
    hook execution policy, prompt transform registry) all read this one
    derivation instead of re-implementing it.
    """
    definition = MODE_DEFINITIONS[mode]
    transform_refs = definition.transform_refs
    if transform_refs:
        from .context_transforms import validate_runtime_context_transform_refs

        transform_refs = validate_runtime_context_transform_refs(
            transform_refs,
            field_path=f"mode {mode} transform_refs",
        )
    return ModeResolution(
        mode=mode,
        read_only=definition.read_only or explicit_read_only,
        transform_refs=transform_refs,
        source=source,
    )


def runtime_read_only_from_metadata(metadata: Mapping[str, object] | None) -> bool:
    if metadata is None:
        return False
    read_only = metadata.get("read_only", False)
    if not isinstance(read_only, bool):
        raise ValueError("runtime read_only must be a boolean")
    return resolve_mode(
        runtime_mode_from_metadata(metadata),
        explicit_read_only=read_only,
    ).read_only
