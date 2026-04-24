from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path


def resolve_workspace_path(*, workspace: Path, raw_path: str) -> tuple[Path, str]:
    workspace_root = workspace.resolve()
    candidate = (workspace_root / Path(raw_path)).resolve()
    if not candidate.is_relative_to(workspace_root):
        raise ValueError("path must be inside the workspace")
    return candidate, candidate.relative_to(workspace_root).as_posix()


def suggest_workspace_paths(*, workspace: Path, raw_path: str, limit: int = 5) -> list[str]:
    workspace_root = workspace.resolve()
    if limit < 1:
        return []

    candidates = [
        path.relative_to(workspace_root).as_posix()
        for path in workspace_root.rglob("*")
        if path.is_file()
    ]
    if not candidates:
        return []

    normalized_path = raw_path.replace("\\", "/")
    suggestions = get_close_matches(normalized_path, candidates, n=limit, cutoff=0.45)
    if suggestions:
        return suggestions

    basename = Path(raw_path).name.lower()
    if basename:
        prefix_matches = [
            candidate
            for candidate in candidates
            if Path(candidate).name.lower().startswith(basename)
        ]
        if prefix_matches:
            return prefix_matches[:limit]

    return candidates[:limit]
