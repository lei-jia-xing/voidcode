from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

type RuntimeMode = Literal["normal", "analyze", "plan"]


def parse_runtime_mode(value: object) -> RuntimeMode:
    if value == "normal":
        return "normal"
    if value == "analyze":
        return "analyze"
    if value == "plan":
        return "plan"
    raise ValueError("runtime mode must be 'normal', 'analyze', or 'plan'")


def runtime_mode_from_metadata(metadata: Mapping[str, object] | None) -> RuntimeMode:
    if metadata is None:
        return "normal"
    return parse_runtime_mode(metadata.get("mode", "normal"))


def runtime_read_only_from_metadata(metadata: Mapping[str, object] | None) -> bool:
    if metadata is None:
        return False
    mode = runtime_mode_from_metadata(metadata)
    if mode in {"analyze", "plan"}:
        return True
    read_only = metadata.get("read_only", False)
    if not isinstance(read_only, bool):
        raise ValueError("runtime read_only must be a boolean")
    return read_only


def legacy_runtime_mode_from_metadata(metadata: Mapping[str, object]) -> RuntimeMode:
    try:
        return runtime_mode_from_metadata(metadata)
    except ValueError:
        return "normal"


def legacy_runtime_read_only_from_metadata(
    metadata: Mapping[str, object],
    *,
    mode: RuntimeMode,
) -> bool:
    if mode in {"analyze", "plan"}:
        return True
    return metadata.get("read_only") is True
