from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .manifest import SkillManifestParseError, parse_skill_manifest
from .models import SkillMetadata

SKILL_ENTRY_FILE_NAME = "SKILL.md"
DEFAULT_SKILL_SEARCH_PATHS = (".voidcode/skills",)


class SkillLoadError(ValueError):
    pass


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
            for entry_path in sorted(skill_root.glob(f"**/{SKILL_ENTRY_FILE_NAME}")):
                if not entry_path.is_file():
                    continue
                discovered.append(self.load(entry_path))

        return tuple(discovered)

    def load(self, entry_path: Path) -> SkillMetadata:
        resolved_entry_path = entry_path.resolve()
        try:
            contents = resolved_entry_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillLoadError(f"failed to read skill file {resolved_entry_path}: {exc}") from exc
        try:
            manifest = parse_skill_manifest(contents, path=str(resolved_entry_path))
            return SkillMetadata(
                name=manifest.name,
                description=manifest.description,
                directory=resolved_entry_path.parent,
                entry_path=resolved_entry_path,
                content=manifest.content,
            )
        except SkillManifestParseError as exc:
            raise SkillLoadError(str(exc)) from exc
        except ValueError as exc:
            raise SkillLoadError(f"{resolved_entry_path}: {exc}") from exc


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
