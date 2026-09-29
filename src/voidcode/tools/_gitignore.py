"""Deliberately small ``.gitignore`` matcher for the workspace scanning tools.

ponytail: honest subset only — the root ``.gitignore`` of the search root is
read (nested ``.gitignore`` files, ``.git/info/exclude`` and the global
``core.excludesFile`` are NOT consulted), and ``**`` relies on ``fnmatch``
rather than full gitignore semantics. Covered: blank/comment lines, ``!``
negation, trailing-``/`` directory patterns (including descendants), leading-``/``
anchoring, and bare-name patterns matching any path segment. Upgrade path: reuse
a shared matcher or shell out to ``git check-ignore`` (which the VCS review
service already does) once nested/global excludes start to matter.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path


class GitIgnoreMatcher:
    """Root-level ``.gitignore`` matcher with last-match-wins negation."""

    def __init__(self, patterns: tuple[str, ...]) -> None:
        self._patterns = patterns

    @classmethod
    def load(cls, root: Path) -> GitIgnoreMatcher | None:
        gitignore = root / ".gitignore"
        if not gitignore.is_file():
            return None
        try:
            lines = gitignore.read_text(encoding="utf-8").splitlines()
        except OSError, UnicodeDecodeError:
            return None
        patterns = tuple(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))
        return cls(patterns) if patterns else None

    @property
    def patterns(self) -> tuple[str, ...]:
        return self._patterns

    def is_ignored(self, relative_path: str, *, is_directory: bool) -> bool:
        normalized = relative_path.strip("/")
        if not normalized:
            return False
        ignored = False
        for pattern in self._patterns:
            negated = pattern.startswith("!")
            candidate = pattern[1:] if negated else pattern
            if _matches(normalized, candidate, is_directory=is_directory):
                ignored = not negated
        return ignored


def _matches(relative_path: str, pattern: str, *, is_directory: bool) -> bool:
    if pattern.endswith("/"):
        base = pattern.rstrip("/")
        if not base:
            return False
        parts = Path(relative_path).parts
        directories = parts if is_directory else parts[:-1]
        return any(_match_path("/".join(directories[:end]), base) for end in range(1, len(directories) + 1))
    return _match_path(relative_path, pattern)


def _match_path(relative_path: str, pattern: str) -> bool:
    anchored = pattern.startswith("/")
    pattern = pattern.strip("/")
    if not pattern:
        return False
    parts = Path(relative_path).parts
    if anchored:
        return fnmatchcase(relative_path, pattern)
    if "/" not in pattern:
        return any(fnmatchcase(part, pattern) for part in parts)
    return any(fnmatchcase("/".join(parts[index:]), pattern) for index in range(len(parts)))


__all__ = ["GitIgnoreMatcher"]
