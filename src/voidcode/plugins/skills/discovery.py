"""Workspace skill discovery with per-entry fault isolation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from voidcode.capabilities.frontmatter import load_frontmatter_mapping, split_frontmatter

from .config import DEFAULT_SKILL_SEARCH_PATHS
from .limits import (
    MAX_SKILL_DIAGNOSTICS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_NAME_CHARS,
    MAX_SKILLS,
    bounded_message,
)
from .manifest import SKILL_ENTRY_FILE_NAME, SkillManifestParseError, SkillMetadata, parse_skill_manifest


class SkillLoadError(ValueError):
    """A discoverable skill file exists but cannot be loaded.

    ``name_hint`` lets the registry expose a stable ``skill://`` key (and a path-based
    config rule) for an entry whose own metadata is unusable.
    """

    def __init__(self, message: str, *, name_hint: str | None = None, path: Path | None = None) -> None:
        super().__init__(message)
        self.name_hint = name_hint
        self.path = path


@dataclass(frozen=True, slots=True)
class SkillLoadFailure:
    """One discovered entry that could not be loaded; never aborts healthy siblings."""

    name: str
    path: Path
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", bounded_message(self.name, limit=MAX_SKILL_NAME_CHARS))
        object.__setattr__(self, "message", bounded_message(self.message))


@dataclass(frozen=True, slots=True)
class SkillDiscovery:
    """Result of scanning configured roots: healthy skills, per-entry failures, config errors."""

    skills: tuple[SkillMetadata, ...] = ()
    failures: tuple[SkillLoadFailure, ...] = ()
    config_errors: tuple[str, ...] = ()


def resolve_workspace_relative_path(*, workspace: Path, configured_path: str) -> Path:
    candidate_path = Path(configured_path)
    if candidate_path.is_absolute():
        return candidate_path.resolve()

    resolved_path = (workspace / candidate_path).resolve()
    if not resolved_path.is_relative_to(workspace):
        raise ValueError(f"skill search path escapes workspace: {configured_path}")
    return resolved_path


def _best_effort_skill_name(contents: str) -> str | None:
    """The declared name when frontmatter happens to be parseable; used only for diagnostics."""
    try:
        raw_frontmatter, _body = split_frontmatter(contents)
        payload = load_frontmatter_mapping(raw_frontmatter)
    except ValueError:
        return None
    name = payload.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


class LocalSkillMetadataLoader:
    def discover(
        self,
        *,
        workspace: Path,
        search_paths: Iterable[str] = DEFAULT_SKILL_SEARCH_PATHS,
    ) -> SkillDiscovery:
        resolved_workspace = workspace.resolve()
        discovered: list[SkillMetadata] = []
        failures: list[SkillLoadFailure] = []
        config_errors: list[str] = []
        seen_entries: set[Path] = set()
        hidden_failures = 0
        stopped = False

        def record_failure(failure: SkillLoadFailure) -> None:
            nonlocal hidden_failures
            if len(failures) < MAX_SKILL_DIAGNOSTICS:
                failures.append(failure)
            else:
                hidden_failures += 1

        for search_path in search_paths:
            try:
                relative_root = not Path(search_path).expanduser().is_absolute()
                skill_root = resolve_workspace_relative_path(workspace=resolved_workspace, configured_path=search_path)
            except (OSError, ValueError) as exc:
                config_errors.append(bounded_message(f"search path '{search_path}': {exc}; ignored"))
                continue
            if not skill_root.is_dir():
                continue
            try:
                entries = sorted(skill_root.glob(f"**/{SKILL_ENTRY_FILE_NAME}"))
            except OSError as exc:
                config_errors.append(bounded_message(f"search path '{search_path}': {exc}; ignored"))
                continue
            for entry_path in entries:
                resolved_entry = entry_path.resolve(strict=False)
                # Overlapping roots commonly reach the same file; load it once.
                if resolved_entry in seen_entries:
                    continue
                if relative_root and not resolved_entry.is_relative_to(resolved_workspace):
                    seen_entries.add(resolved_entry)
                    record_failure(
                        SkillLoadFailure(
                            name=entry_path.parent.name or SKILL_ENTRY_FILE_NAME,
                            path=entry_path,
                            message="entry resolves outside the workspace; ignored",
                        )
                    )
                    continue
                if not resolved_entry.is_file():
                    continue
                seen_entries.add(resolved_entry)
                if len(discovered) >= MAX_SKILLS:
                    if len(failures) >= MAX_SKILL_DIAGNOSTICS:
                        config_errors.append(
                            bounded_message(f"skill discovery cap of {MAX_SKILLS} skills reached; further entries under {skill_root} were ignored")
                        )
                        stopped = True
                        break
                    record_failure(
                        SkillLoadFailure(
                            name=entry_path.parent.name or SKILL_ENTRY_FILE_NAME,
                            path=entry_path,
                            message=f"not loaded: discovery cap of {MAX_SKILLS} skills reached",
                        )
                    )
                    continue
                try:
                    discovered.append(self.load(resolved_entry))
                except (SkillLoadError, OSError) as exc:
                    hint = getattr(exc, "name_hint", None) or entry_path.parent.name or SKILL_ENTRY_FILE_NAME
                    record_failure(SkillLoadFailure(name=hint, path=resolved_entry, message=str(exc)))
            if stopped:
                break

        if hidden_failures:
            config_errors.append(bounded_message(f"{hidden_failures} additional broken or over-cap skill entr(ies) were not loaded or reported"))
        return SkillDiscovery(
            skills=tuple(discovered),
            failures=tuple(failures),
            config_errors=tuple(config_errors[:MAX_SKILL_DIAGNOSTICS]),
        )

    def load(self, entry_path: Path) -> SkillMetadata:
        resolved_entry_path = entry_path.resolve()
        try:
            with resolved_entry_path.open("rb") as handle:
                data = handle.read(MAX_SKILL_FILE_BYTES + 1)
        except OSError as exc:
            raise SkillLoadError(
                f"failed to read skill file {resolved_entry_path}: {bounded_message(exc)}",
                path=resolved_entry_path,
            ) from exc
        if len(data) > MAX_SKILL_FILE_BYTES:
            raise SkillLoadError(
                f"skill file exceeds the {MAX_SKILL_FILE_BYTES}-byte cap; keep SKILL.md below that size",
                path=resolved_entry_path,
            )
        try:
            contents = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLoadError(f"skill file is not valid UTF-8 (invalid byte at offset {exc.start})", path=resolved_entry_path) from exc
        try:
            return parse_skill_manifest(contents, entry_path=resolved_entry_path)
        except SkillManifestParseError as exc:
            name_hint = _best_effort_skill_name(contents)
            raise SkillLoadError(str(exc), name_hint=name_hint, path=resolved_entry_path) from exc
