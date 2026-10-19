"""Plugin-local resource loading.

Data files ship inside the admitted plugin artifact next to these modules, so a
plain filesystem-relative read is both correct and independent of how the host
names the plugin package.
"""

from __future__ import annotations

import json
import sysconfig
from pathlib import Path

_RESOURCE_ROOT = Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Absolute path of one plugin-local resource (JSON data or data directory)."""
    path = (_RESOURCE_ROOT / name).resolve()
    if not path.is_relative_to(_RESOURCE_ROOT):
        raise ValueError(f"plugin resource escapes its artifact root: {name}")
    return path


def resource_text(name: str) -> str:
    if name.endswith(".so") or name.endswith(".pyd"):
        raise ValueError("native extensions are not plugin resources")
    return resource_path(name).read_text(encoding="utf-8")


def load_json(name: str) -> object:
    return json.loads(resource_text(name))


def stdlib_data_path(suffix: str) -> Path:
    """Locate one installed data package (tiktoken vocabularies, tokenizer data)."""
    return Path(sysconfig.get_paths()["purelib"]) / suffix
