"""Strict workflow-name to workspace-relative script-path configuration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType


def parse_workflows_config(config: Mapping[str, object] | None) -> Mapping[str, str]:
    """Configured workflow scripts: {name: workspace-relative .py path}."""
    if config is None:
        return MappingProxyType({})
    unknown = sorted(str(key) for key in config if key not in ("workflows", "providers"))
    if unknown:
        raise ValueError(f"workflows config field '{unknown[0]}' is not supported")
    raw = config.get("workflows", {})
    if not isinstance(raw, Mapping):
        raise ValueError("workflows config 'workflows' must be an object")
    resolved: dict[str, str] = {}
    for name, path in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("workflow names must be non-empty strings")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"workflow '{name}' path must be a non-empty string")
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix != ".py":
            raise ValueError(f"workflow '{name}' path must be a workspace-relative .py file without '..'")
        if name in resolved:
            raise ValueError(f"duplicate workflow name: {name!r}")
        resolved[name] = path
    return MappingProxyType(resolved)
