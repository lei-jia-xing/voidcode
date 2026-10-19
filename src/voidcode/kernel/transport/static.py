"""Locate the built web client the kernel transport serves.

The transport serves one static bundle: in a source checkout the freshly built
``frontend/dist``, otherwise the ``voidcode/_web_dist`` copy the wheel stages at
build time. A layout the process cannot hand to the filesystem (a zipped
install) is reported as absent rather than extracted here.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

PACKAGED_DIST_NAME = "_web_dist"
#: ``src/voidcode/kernel/transport/static.py`` -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]


def frontend_distribution() -> Path | None:
    """The web bundle this installation serves, or ``None`` when none is built."""
    repo_dist = _REPO_ROOT / "frontend" / "dist"
    if (repo_dist / "index.html").is_file():
        return repo_dist
    packaged = importlib.resources.files("voidcode").joinpath(PACKAGED_DIST_NAME)
    if isinstance(packaged, Path) and (packaged / "index.html").is_file():
        return packaged
    return None
