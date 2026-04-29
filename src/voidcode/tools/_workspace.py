from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path

from ..security.path_policy import resolve_workspace_path as _resolve_workspace_path


def resolve_workspace_path(*, workspace: Path, raw_path: str) -> tuple[Path, str]:
    resolution = _resolve_workspace_path(
        workspace=workspace,
        raw_path=raw_path,
        containment_error="path must be inside the workspace",
    )
    return resolution.candidate, resolution.relative_path


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
