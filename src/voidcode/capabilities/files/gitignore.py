"""Nested ``.gitignore`` matching with Git's precedence rules.

Each ``.gitignore`` applies relative to its own directory: anchored patterns
are anchored there and unanchored patterns float below it. The search root's
``.git/info/exclude`` is the lowest-precedence source, followed by the root
``.gitignore``, then each deeper ``.gitignore`` in path order; the last matching
rule across that stack wins. Sources are loaded lazily as the walker reaches
their directory, so ignored subtrees are never scanned for ignore files.

A line pathspec rejects (a bare ``!``, an invalid character range, or a trailing
escape) is escaped into a literal pattern instead of aborting the search or
being silently dropped. Git has no invalid-pattern diagnostic, so matching the
line literally is the closest bounded behavior. An unreadable ignore file still
raises a typed tool diagnostic. Global excludes (``core.excludesFile``) are out
of scope: only the search root's ``.git/info/exclude`` participates.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from pathlib import Path

from pathspec import GitIgnoreSpec
from pathspec.patterns.gitignore.spec import GitIgnoreSpecPattern

from ..tool_errors import raise_tool_diagnostic

_IGNORE_FILE = ".gitignore"
_EXCLUDE_FILE = Path(".git/info/exclude")


def _literal_pattern(line: str) -> str:
    """Escape a rejected ignore line so it matches literal text."""
    stripped = line.rstrip(" ")
    escaped = "".join(
        f"\\{character}" if character in "\\*?[" or (index == 0 and character in "!#") else character for index, character in enumerate(stripped)
    )
    return escaped + "\\ " * (len(line) - len(stripped))


def _is_contents_only_pattern(pattern: object) -> bool:
    """True for a trailing ``/**`` rule, which matches a directory's contents."""
    if not isinstance(pattern, str):
        return False
    body = pattern.strip()
    if body.startswith("!"):
        body = body[1:]
    if body.endswith("/"):
        body = body[:-1]
    return body == "**" or body.endswith("/**")


def _compile_lines(lines: Iterable[str]) -> list[GitIgnoreSpecPattern]:
    patterns: list[GitIgnoreSpecPattern] = []
    for line in lines:
        if not line:
            continue
        try:
            spec = GitIgnoreSpec.from_lines([line])
        except Exception:  # noqa: BLE001 - pathspec reports malformed input as ValueError or re.error
            try:
                spec = GitIgnoreSpec.from_lines([_literal_pattern(line)])
            except Exception:  # noqa: BLE001 - a line that cannot even be escaped is skipped
                continue
        patterns.extend(spec.patterns)
    return patterns


class GitIgnoreMatcher:
    """A lazily loaded stack of ``.gitignore`` and root exclude rules."""

    def __init__(self, patterns: Collection[str] = (), *, root: Path | None = None) -> None:
        self._root = root
        self._specs: dict[str, tuple[GitIgnoreSpec, ...]] = {}
        self._ignored: dict[tuple[str, bool], bool] = {}
        if patterns:
            compiled = _compile_lines(patterns)
            self._specs[""] = (GitIgnoreSpec(compiled),) if compiled else ()

    @classmethod
    def load(cls, root: Path) -> GitIgnoreMatcher:
        """Build a matcher rooted at ``root`` and load its root-level sources eagerly."""
        matcher = cls(root=root)
        matcher._directory_specs("")
        return matcher

    def _read_spec(self, path: Path) -> GitIgnoreSpec | None:
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise_tool_diagnostic(
                message=f"Cannot read ignore rules from {path}: {exc}",
                error_kind="tool_execution_error",
                reason="ignore_file_read_failed",
            )
        patterns = _compile_lines(lines)
        return GitIgnoreSpec(patterns) if patterns else None

    def _directory_specs(self, relative_directory: str) -> tuple[GitIgnoreSpec, ...]:
        cached = self._specs.get(relative_directory)
        if cached is not None:
            return cached
        specs: list[GitIgnoreSpec] = []
        if self._root is not None:
            directory = self._root if not relative_directory else self._root / relative_directory
            if not relative_directory:
                exclude = self._read_spec(self._root / _EXCLUDE_FILE)
                if exclude is not None:
                    specs.append(exclude)
            ignore = self._read_spec(directory / _IGNORE_FILE)
            if ignore is not None:
                specs.append(ignore)
        result = tuple(specs)
        self._specs[relative_directory] = result
        return result

    @staticmethod
    def _spec_decision(spec: GitIgnoreSpec, relative: str, *, is_directory: bool) -> bool | None:
        """The spec's last-word decision for one path, or ``None`` when unrivaled."""
        if not is_directory:
            result = spec.check_file(relative)
            return None if result.include is None else bool(result.include)
        plain = spec.check_file(relative)
        slashed = spec.check_file(relative + "/")
        plain_match = (plain.index, bool(plain.include)) if plain.include is not None and plain.index is not None else None
        slash_match = (slashed.index, bool(slashed.include)) if slashed.include is not None and slashed.index is not None else None
        if plain_match is not None and slash_match is not None:
            # A later directory-only rule may unignore a directory that an
            # earlier contents pattern matched; prefer the later rule.
            if slash_match[0] > plain_match[0]:
                return slash_match[1]
            return plain_match[1]
        if plain_match is not None:
            return plain_match[1]
        if slash_match is not None:
            index, decision = slash_match
            pattern = spec.patterns[index].pattern
            # ``dir/**`` matches the directory when queried with a trailing slash
            # but Git still descends into it so direct children can be re-included.
            if _is_contents_only_pattern(pattern):
                return None
            return decision
        return None

    def _matches(self, parts: tuple[str, ...], *, is_directory: bool) -> bool:
        normalized = "/".join(parts)
        ignored = False
        for depth in range(len(parts)):
            prefix = "/".join(parts[:depth])
            specs = self._directory_specs(prefix)
            if not specs:
                continue
            relative = normalized if not prefix else normalized[len(prefix) + 1 :]
            for spec in specs:
                decision = self._spec_decision(spec, relative, is_directory=is_directory)
                if decision is not None:
                    ignored = decision
        return ignored

    def _is_ignored(self, parts: tuple[str, ...], *, is_directory: bool) -> bool:
        key = ("/".join(parts), is_directory)
        cached = self._ignored.get(key)
        if cached is not None:
            return cached
        # Git never re-includes a path whose ancestor directory is excluded,
        # even when a deeper ignore file names the path with ``!``. Checking
        # ancestors first also keeps ignore files below an excluded directory
        # unread.
        for depth in range(1, len(parts)):
            if self._is_ignored(parts[:depth], is_directory=True):
                self._ignored[key] = True
                return True
        ignored = self._matches(parts, is_directory=is_directory)
        self._ignored[key] = ignored
        return ignored

    def is_ignored(self, relative_path: str, *, is_directory: bool) -> bool:
        normalized = relative_path.strip("/")
        if not normalized or normalized == ".":
            return False
        parts = tuple(normalized.split("/"))
        if any(part in ("", ".", "..") for part in parts):
            return False
        return self._is_ignored(parts, is_directory=is_directory)


__all__ = ["GitIgnoreMatcher"]
