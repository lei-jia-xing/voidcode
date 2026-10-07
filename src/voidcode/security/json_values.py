from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType


def own_json_value(value: object) -> object:
    """Validate and take one immutable snapshot of a JSON value."""
    if isinstance(value, Mapping):
        return own_json_object(value)
    if isinstance(value, (list, tuple)):
        return tuple(own_json_value(item) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON values must contain finite numbers")
        return value
    raise ValueError(f"cannot snapshot non-JSON value: {type(value).__name__}")


def own_json_object(value: Mapping[str, object]) -> Mapping[str, object]:
    owned: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("JSON object keys must be strings")
        owned[key] = own_json_value(item)
    return MappingProxyType(owned)


def json_wire_value(value: object) -> object:
    """Project immutable JSON containers only at a serialization boundary."""
    if isinstance(value, Mapping):
        return {key: json_wire_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [json_wire_value(item) for item in value]
    return value


def json_wire_object(value: Mapping[str, object]) -> dict[str, object]:
    return {key: json_wire_value(item) for key, item in value.items()}
