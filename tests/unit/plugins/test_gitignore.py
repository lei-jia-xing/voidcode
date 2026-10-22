"""Git ignore syntax supported by workspace searches."""

from pathlib import Path

import pytest

from voidcode.capabilities.files.gitignore import GitIgnoreMatcher


@pytest.mark.parametrize(
    ("pattern", "path", "is_directory", "expected"),
    [
        ("/build", "build/output.txt", False, True),
        ("/build", "src/build/output.txt", False, False),
        ("src/generated", "nested/src/generated", False, False),
        ("src/generated", "src/generated/output.txt", False, True),
        ("a/**/b.txt", "a/b.txt", False, True),
        ("a/**/b.txt", "a/x/y/b.txt", False, True),
        ("a/*/b.txt", "a/x/y/b.txt", False, False),
        ("cache/", "cache", True, True),
        ("cache/", "cache", False, False),
        ("cache/", "src/cache/data.txt", False, True),
        (r"\#notes", "#notes", False, True),
        (r"\!keep", "!keep", False, True),
        ("name\\ ", "name ", False, True),
        ("# comment", "# comment", False, False),
    ],
)
def test_gitignore_syntax(pattern: str, path: str, is_directory: bool, expected: bool) -> None:
    matcher = GitIgnoreMatcher((pattern,))
    assert matcher.is_ignored(path, is_directory=is_directory) is expected


def test_negation_and_rule_order() -> None:
    matcher = GitIgnoreMatcher(("*.log", "!keep.log", "/keep.log"))
    assert matcher.is_ignored("output.log", is_directory=False)
    assert not matcher.is_ignored("sub/keep.log", is_directory=False)
    assert matcher.is_ignored("keep.log", is_directory=False)


def test_load_preserves_escaped_whitespace_and_reads_only_root(tmp_path: Path) -> None:
    assert GitIgnoreMatcher.load(tmp_path) is None
    (tmp_path / ".gitignore").write_text("name\\ \n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/.gitignore").write_text("other\n", encoding="utf-8")
    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher is not None
    assert matcher.is_ignored("name ", is_directory=False)
    assert not matcher.is_ignored("sub/other", is_directory=False)
