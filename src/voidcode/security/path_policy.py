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
    require_existing: bool = False,
    existence_error: str | None = None,
    require_regular_file: bool = False,
    regular_file_error: str | None = None,
    allow_outside_workspace: bool = False,
) -> WorkspacePathResolution:
    workspace_root = workspace.resolve()
    path_candidate = Path(raw_path).expanduser()
    candidate = (
        path_candidate.resolve()
        if path_candidate.is_absolute()
        else (workspace_root / path_candidate).resolve()
    )

    is_external = not candidate.is_relative_to(workspace_root)
    if is_external and not allow_outside_workspace:
        raise ValueError(containment_error)

    if require_existing and not candidate.exists():
        raise ValueError(existence_error or f"target does not exist: {raw_path}")

    if require_regular_file and not candidate.is_file():
        raise ValueError(regular_file_error or f"target is not a regular file: {raw_path}")

    relative_path = (
        candidate.relative_to(workspace_root).as_posix()
        if not is_external
        else candidate.as_posix()
    )

    return WorkspacePathResolution(
        workspace_root=workspace_root,
        candidate=candidate,
        relative_path=relative_path,
        is_external=is_external,
    )


__all__ = ["WorkspacePathResolution", "resolve_workspace_path"]
