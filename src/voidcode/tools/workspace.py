from __future__ import annotations

from pathlib import Path


def workspace_root(workspace: Path) -> Path:
    return workspace.resolve()


def resolve_workspace_path(
    *,
    workspace: Path,
    path_text: str,
    tool_name: str,
    subject: str = "path",
    must_exist: bool = True,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> tuple[Path, str]:
    root = workspace_root(workspace)
    candidate = (root / Path(path_text)).resolve()

    if not candidate.is_relative_to(root):
        raise ValueError(f"{tool_name} only allows paths inside the workspace")

    if must_exist and not candidate.exists():
        raise ValueError(f"{tool_name} target does not exist: {path_text}")

    if must_be_file and not candidate.is_file():
        raise ValueError(f"{tool_name} target is not a file: {path_text}")

    if must_be_dir and not candidate.is_dir():
        raise ValueError(f"{tool_name} target is not a directory: {path_text}")

    return candidate, candidate.relative_to(root).as_posix()
