"""Backward-compatible facade for the runtime HTTP transport.

The implementation lives in :mod:`voidcode.runtime.transport.http`; this
module remains importable so existing integrations keep their import path.
"""

from importlib import import_module
from typing import Any

_impl = import_module(".transport.http", __package__)

# Keep the historical module surface for public HTTP symbols.
for _name in dir(_impl):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_impl, _name)


def __getattr__(name: str) -> Any:
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))
