from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast


class _ToolResultLike(Protocol):
    data: dict[str, object]


README_FILE_NAME = "README.md"
MAX_README_FILES = 8
MAX_README_FILE_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class DirectoryReadmeContext:
    path: str
    content: str
    truncated: bool = False

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": "directory_readme_context",
            "path": self.path,
        }
        if self.truncated:
            payload["truncated"] = True
        return payload


def directory_readme_contexts(
    *,
    workspace: Path | None,
    tool_results: tuple[_ToolResultLike, ...],
    max_readme_files: int = MAX_README_FILES,
    max_readme_file_chars: int = MAX_README_FILE_CHARS,
    include_workspace_root: bool = True,
) -> tuple[DirectoryReadmeContext, ...]:
    if workspace is None or max_readme_files < 1:
        return ()
    workspace_root = workspace.resolve(strict=False)
    readme_paths = _applicable_readme_paths(
        workspace_root=workspace_root,
        touched_paths=_touched_paths_from_tool_results(tool_results),
        max_readme_files=max_readme_files,
        include_workspace_root=include_workspace_root,
    )
    contexts: list[DirectoryReadmeContext] = []
    for readme_path in readme_paths:
        try:
            content = readme_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        truncated = len(content) > max_readme_file_chars
        if truncated:
            content = content[:max_readme_file_chars].rstrip()
            content = f"{content}\n[README context truncated by runtime context policy]"
        contexts.append(
            DirectoryReadmeContext(
                path=readme_path.relative_to(workspace_root).as_posix(),
                content=content.strip(),
                truncated=truncated,
            )
        )
    return tuple(contexts)


def _touched_paths_from_tool_results(tool_results: tuple[_ToolResultLike, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()

    def append(value: object) -> None:
        if not isinstance(value, str):
            return
        stripped = value.strip()
        if not stripped:
            return
        if stripped in seen:
            paths.remove(stripped)
        else:
            seen.add(stripped)
        paths.append(stripped)

    for result in tool_results:
        append(result.data.get("path"))
        append(result.data.get("output_path"))
        raw_arguments = result.data.get("arguments")
        if isinstance(raw_arguments, dict):
            arguments = cast(dict[str, object], raw_arguments)
            append(arguments.get("path"))
            append(arguments.get("filePath"))
        raw_matches = result.data.get("matches")
        if isinstance(raw_matches, list | tuple):
            for raw_match in raw_matches:
                if not isinstance(raw_match, dict):
                    continue
                match = cast(dict[str, object], raw_match)
                append(match.get("file"))
                append(match.get("path"))
        raw_changes = result.data.get("changes")
        if isinstance(raw_changes, list | tuple):
            for raw_change in raw_changes:
                if not isinstance(raw_change, dict):
                    continue
                change = cast(dict[str, object], raw_change)
                append(change.get("path"))
    return tuple(paths)


def _applicable_readme_paths(
    *,
    workspace_root: Path,
    touched_paths: tuple[str, ...],
    max_readme_files: int,
    include_workspace_root: bool,
) -> tuple[Path, ...]:
    ordered: list[Path] = []
    seen: set[Path] = set()

    root_readme_path = workspace_root / README_FILE_NAME
    if include_workspace_root and root_readme_path.is_file():
        seen.add(root_readme_path)
        ordered.append(root_readme_path)

    for raw_path in touched_paths:
        touched_path = _workspace_path(workspace_root=workspace_root, raw_path=raw_path)
        if touched_path is None:
            continue
        for readme_path in _candidate_readme_paths(
            workspace_root=workspace_root,
            path=touched_path,
        ):
            if readme_path in seen or not readme_path.is_file():
                continue
            seen.add(readme_path)
            ordered.append(readme_path)
    if len(ordered) <= max_readme_files:
        return tuple(ordered)
    return tuple(ordered[-max_readme_files:])


def _workspace_path(*, workspace_root: Path, raw_path: str) -> Path | None:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    return resolved


def _candidate_readme_paths(*, workspace_root: Path, path: Path) -> tuple[Path, ...]:
    start = path if path.is_dir() else path.parent
    directories: list[Path] = []
    current = start
    while True:
        try:
            current.relative_to(workspace_root)
        except ValueError:
            break
        directories.append(current)
        if current == workspace_root:
            break
        current = current.parent
    return tuple(directory / README_FILE_NAME for directory in reversed(directories))
