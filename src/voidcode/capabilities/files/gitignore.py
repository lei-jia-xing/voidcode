"""Root .gitignore matching using pathspec's Git ignore semantics.

Only the search root's .gitignore is loaded. Nested ignore files,
.git/info/exclude and global excludes are outside this scanner's scope.
"""

from __future__ import annotations

from pathlib import Path

from pathspec import GitIgnoreSpec


class GitIgnoreMatcher:
    """Root-level ``.gitignore`` matcher with last-match-wins negation."""

    def __init__(self, patterns: tuple[str, ...]) -> None:
        self._patterns = patterns
        self._spec = GitIgnoreSpec.from_lines(patterns)

    @classmethod
    def load(cls, root: Path) -> GitIgnoreMatcher | None:
        gitignore = root / ".gitignore"
        if not gitignore.is_file():
            return None
        try:
            lines = gitignore.read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeDecodeError:
            return None
        patterns = tuple(lines)
        return cls(patterns) if patterns else None

    @property
    def patterns(self) -> tuple[str, ...]:
        return self._patterns

    def is_ignored(self, relative_path: str, *, is_directory: bool) -> bool:
        normalized = relative_path.strip("/")
        if not normalized:
            return False
        candidate = normalized + "/" if is_directory else normalized
        return self._spec.match_file(candidate)


__all__ = ["GitIgnoreMatcher"]
