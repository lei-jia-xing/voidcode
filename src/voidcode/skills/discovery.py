from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .manifest import parse_skill_body, parse_skill_frontmatter
from .models import SkillMetadata

SKILL_ENTRY_FILE_NAME = "SKILL.md"
DEFAULT_SKILL_SEARCH_PATHS = (".voidcode/skills",)


class LocalSkillMetadataLoader:
    def discover(
        self,
        *,
        workspace: Path,
        search_paths: Iterable[str] = DEFAULT_SKILL_SEARCH_PATHS,
    ) -> tuple[SkillMetadata, ...]:
        resolved_workspace = workspace.resolve()
        discovered: list[SkillMetadata] = []

        for search_path in search_paths:
            skill_root = resolve_workspace_relative_path(
                workspace=resolved_workspace, configured_path=search_path
            )
            if not skill_root.exists() or not skill_root.is_dir():
                continue
            for skill_dir in sorted(path for path in skill_root.iterdir() if path.is_dir()):
                entry_path = skill_dir / SKILL_ENTRY_FILE_NAME
                if not entry_path.exists() or not entry_path.is_file():
                    continue
                discovered.append(self.load(entry_path))

        return tuple(discovered)

    def load(self, entry_path: Path) -> SkillMetadata:
        resolved_entry_path = entry_path.resolve()
        contents = resolved_entry_path.read_text(encoding="utf-8")
        metadata = parse_skill_frontmatter(contents)
        return SkillMetadata(
            name=metadata["name"],
            description=metadata["description"],
            directory=resolved_entry_path.parent,
            entry_path=resolved_entry_path,
            content=parse_skill_body(contents),
        )


def resolve_workspace_relative_path(*, workspace: Path, configured_path: str) -> Path:
    candidate_path = Path(configured_path)
    if candidate_path.is_absolute():
        return candidate_path.resolve()

    resolved_path = (workspace / candidate_path).resolve()
    try:
        resolved_path.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"skill search path escapes workspace: {configured_path}") from exc
    return resolved_path
