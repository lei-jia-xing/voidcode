from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def discover_workspace_root(
    *,
    file_path: Path,
    workspace_root: Path,
    root_markers: Iterable[str],
) -> Path:
    resolved_workspace_root = workspace_root.resolve()
    resolved_file_path = file_path.resolve()
    markers = tuple(marker for marker in root_markers if marker)
    if not markers:
        return resolved_workspace_root

    try:
        resolved_file_path.relative_to(resolved_workspace_root)
    except ValueError:
        return resolved_workspace_root

    search_dir = resolved_file_path if resolved_file_path.is_dir() else resolved_file_path.parent
    current = search_dir

    while True:
        if any((current / marker).exists() for marker in markers):
            return current
        if current == resolved_workspace_root:
            return resolved_workspace_root
        parent = current.parent
        if parent == current:
            return resolved_workspace_root
        try:
            parent.relative_to(resolved_workspace_root)
        except ValueError:
            return resolved_workspace_root
        current = parent
