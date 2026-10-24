"""Locate the built web client one installation serves.

The transport serves one static bundle: in a source checkout the freshly built
``frontend/dist``, otherwise the ``voidcode/_web_dist`` copy the wheel stages at
build time. A layout the process cannot hand to the filesystem (a zipped
install) is reported as absent rather than extracted here.
"""

from __future__ import annotations

from pathlib import Path

PACKAGED_DIST_NAME = "_web_dist"


def distribution_for(package_root: Path) -> Path | None:
    """The web bundle belonging to one installation's package tree, or ``None`` when none is built.

    A source checkout serves ``frontend/dist``; an installed package serves the
    bundle staged inside the package as ``_web_dist``.
    """
    package_root = Path(package_root)
    if package_root.parent.name == "src":
        repo_dist = package_root.parent.parent / "frontend" / "dist"
        if (repo_dist / "index.html").is_file():
            return repo_dist
    packaged = package_root / PACKAGED_DIST_NAME
    if (packaged / "index.html").is_file():
        return packaged
    return None


def frontend_distribution() -> Path | None:
    """The web bundle this installation serves, or ``None`` when none is built."""
    #: ``src/voidcode/core/transport/static.py`` -> the running package tree.
    return distribution_for(Path(__file__).resolve().parents[2])
