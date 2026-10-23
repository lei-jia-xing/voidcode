"""Packaged provider resource loading.

Data files ship next to these modules, so a
plain filesystem-relative read is both correct and independent of how the runtime
names the plugin package.
"""

from __future__ import annotations

import json
from pathlib import Path

_RESOURCE_ROOT = Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Absolute path of one packaged provider resource (JSON data or data directory)."""
    path = (_RESOURCE_ROOT / name).resolve()
    if not path.is_relative_to(_RESOURCE_ROOT):
        raise ValueError(f"provider resource escapes its package root: {name}")
    return path


def load_json(name: str) -> object:
    return json.loads(resource_path(name).read_text(encoding="utf-8"))
