from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspacePathResolution:
    workspace_root: Path
    candidate: Path
    relative_path: str
    is_external: bool = False


def resolve_workspace_path(
    *,
    workspace: Path,
    raw_path: str,
    containment_error: str = "path must be inside the workspace",
    allow_outside_workspace: bool = False,
) -> WorkspacePathResolution:
    workspace_root = workspace.resolve()
    path_candidate = Path(raw_path)
    try:
        path_candidate = path_candidate.expanduser()
    except RuntimeError:
        path_candidate = Path(raw_path)
    candidate = path_candidate.resolve() if path_candidate.is_absolute() else (workspace_root / path_candidate).resolve()

    is_external = not candidate.is_relative_to(workspace_root)
    if is_external and not allow_outside_workspace:
        raise ValueError(containment_error)

    relative_path = candidate.relative_to(workspace_root).as_posix() if not is_external else candidate.as_posix()

    return WorkspacePathResolution(
        workspace_root=workspace_root,
        candidate=candidate,
        relative_path=relative_path,
        is_external=is_external,
    )


__all__ = ["WorkspacePathResolution", "resolve_workspace_path"]
