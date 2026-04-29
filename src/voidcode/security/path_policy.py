from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspacePathResolution:
    workspace_root: Path
    candidate: Path
    relative_path: str


def resolve_workspace_path(
    *,
    workspace: Path,
    raw_path: str,
    containment_error: str = "path must be inside the workspace",
    require_existing: bool = False,
    existence_error: str | None = None,
    require_regular_file: bool = False,
    regular_file_error: str | None = None,
) -> WorkspacePathResolution:
    workspace_root = workspace.resolve()
    candidate = (workspace_root / Path(raw_path)).resolve()

    if not candidate.is_relative_to(workspace_root):
        raise ValueError(containment_error)

    if require_existing and not candidate.exists():
        raise ValueError(existence_error or f"target does not exist: {raw_path}")

    if require_regular_file and not candidate.is_file():
        raise ValueError(regular_file_error or f"target is not a regular file: {raw_path}")

    return WorkspacePathResolution(
        workspace_root=workspace_root,
        candidate=candidate,
        relative_path=candidate.relative_to(workspace_root).as_posix(),
    )


__all__ = ["WorkspacePathResolution", "resolve_workspace_path"]
