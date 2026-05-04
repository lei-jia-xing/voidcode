from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..tools.contracts import ToolResult

RULE_FILE_NAME = "AGENTS.md"
MAX_RULE_FILES = 8
MAX_RULE_FILE_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class RuntimeFileRuleContext:
    path: str
    content: str
    truncated: bool = False

    def metadata_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source": "runtime_file_rules",
            "path": self.path,
        }
        if self.truncated:
            payload["truncated"] = True
        return payload


def runtime_file_rule_contexts(
    *,
    workspace: Path | None,
    tool_results: tuple[ToolResult, ...],
    max_rule_files: int = MAX_RULE_FILES,
    max_rule_file_chars: int = MAX_RULE_FILE_CHARS,
) -> tuple[RuntimeFileRuleContext, ...]:
    if workspace is None or max_rule_files < 1:
        return ()
    workspace_root = workspace.resolve(strict=False)
    rule_paths = _applicable_rule_paths(
        workspace_root=workspace_root,
        touched_paths=_touched_paths_from_tool_results(tool_results),
        max_rule_files=max_rule_files,
    )
    contexts: list[RuntimeFileRuleContext] = []
    for rule_path in rule_paths:
        try:
            content = rule_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        truncated = len(content) > max_rule_file_chars
        if truncated:
            content = content[:max_rule_file_chars].rstrip()
            content = f"{content}\n[Rule file truncated by runtime context policy]"
        contexts.append(
            RuntimeFileRuleContext(
                path=rule_path.relative_to(workspace_root).as_posix(),
                content=content.strip(),
                truncated=truncated,
            )
        )
    return tuple(contexts)


def _touched_paths_from_tool_results(tool_results: tuple[ToolResult, ...]) -> tuple[str, ...]:
    paths: list[str] = []
    seen: set[str] = set()

    def append(value: object) -> None:
        if not isinstance(value, str):
            return
        stripped = value.strip()
        if not stripped or stripped in seen:
            return
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
    return tuple(paths)


def _applicable_rule_paths(
    *, workspace_root: Path, touched_paths: tuple[str, ...], max_rule_files: int
) -> tuple[Path, ...]:
    ordered: list[Path] = []
    seen: set[Path] = set()
    for raw_path in touched_paths:
        touched_path = _workspace_path(workspace_root=workspace_root, raw_path=raw_path)
        if touched_path is None:
            continue
        for rule_path in _candidate_rule_paths(workspace_root=workspace_root, path=touched_path):
            if rule_path in seen or not rule_path.is_file():
                continue
            seen.add(rule_path)
            ordered.append(rule_path)
    if len(ordered) <= max_rule_files:
        return tuple(ordered)
    return tuple(ordered[-max_rule_files:])


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


def _candidate_rule_paths(*, workspace_root: Path, path: Path) -> tuple[Path, ...]:
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
    return tuple(directory / RULE_FILE_NAME for directory in reversed(directories))
