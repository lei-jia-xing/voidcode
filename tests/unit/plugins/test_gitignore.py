"""Git ignore syntax and the nested ignore stack supported by workspace searches."""

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


def test_nested_files_are_relative_to_their_directory_and_loaded_on_demand(tmp_path: Path) -> None:
    assert GitIgnoreMatcher.load(tmp_path).is_ignored("sub/other", is_directory=False) is False
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("other\n/anchored.txt\nfloating.txt\nouter/inner.txt\n", encoding="utf-8")

    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher.is_ignored("sub/other", is_directory=False)
    assert matcher.is_ignored("sub/anchored.txt", is_directory=False)
    assert not matcher.is_ignored("sub/deep/anchored.txt", is_directory=False)
    assert matcher.is_ignored("sub/floating.txt", is_directory=False)
    assert matcher.is_ignored("sub/deep/floating.txt", is_directory=False)
    assert matcher.is_ignored("sub/outer/inner.txt", is_directory=False)
    assert not matcher.is_ignored("sub/deep/outer/inner.txt", is_directory=False)
    assert not matcher.is_ignored("other", is_directory=False)


def test_nested_directory_only_and_double_star_patterns(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("cache/\na/**/b.txt\n", encoding="utf-8")

    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher.is_ignored("sub/cache", is_directory=True)
    assert not matcher.is_ignored("sub/cache", is_directory=False)
    assert matcher.is_ignored("sub/cache/data.txt", is_directory=False)
    assert matcher.is_ignored("sub/deep/cache", is_directory=True)
    assert matcher.is_ignored("sub/a/b.txt", is_directory=False)
    assert matcher.is_ignored("sub/a/x/y/b.txt", is_directory=False)
    assert not matcher.is_ignored("sub/a/x/c.txt", is_directory=False)


def test_trailing_double_star_ignores_contents_but_not_the_directory_itself(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("a/**\n!a/keep.txt\n", encoding="utf-8")

    matcher = GitIgnoreMatcher.load(tmp_path)
    assert not matcher.is_ignored("sub/a", is_directory=True)
    assert not matcher.is_ignored("sub/a/keep.txt", is_directory=False)
    assert matcher.is_ignored("sub/a/x", is_directory=True)
    assert matcher.is_ignored("sub/a/x/y.txt", is_directory=False)


def test_deeper_files_override_parent_rules_and_last_match_wins(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n!sub/keep.log\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("keep.log\n!keep.log\n", encoding="utf-8")

    matcher = GitIgnoreMatcher.load(tmp_path)
    assert not matcher.is_ignored("sub/keep.log", is_directory=False)
    assert matcher.is_ignored("sub/drop.log", is_directory=False)

    (tmp_path / "sub" / ".gitignore").write_text("!keep.log\nkeep.log\n", encoding="utf-8")
    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher.is_ignored("sub/keep.log", is_directory=False)


def test_info_exclude_is_the_lowest_precedence_source(tmp_path: Path) -> None:
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("*.log\n!keep.log\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("!drop.log\n", encoding="utf-8")

    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher.is_ignored("other.log", is_directory=False)
    assert not matcher.is_ignored("drop.log", is_directory=False)
    assert not matcher.is_ignored("keep.log", is_directory=False)

    (tmp_path / ".gitignore").write_text("drop.log\n", encoding="utf-8")
    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher.is_ignored("drop.log", is_directory=False)


def test_malformed_patterns_are_applied_literally(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("!\na[z-a]\n\\\n", encoding="utf-8")

    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher.is_ignored("!", is_directory=False)
    assert matcher.is_ignored("a[z-a]", is_directory=False)
    assert matcher.is_ignored("\\", is_directory=False)
    assert not matcher.is_ignored("other", is_directory=False)


def test_hidden_entries_are_not_ignored_without_a_rule(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    matcher = GitIgnoreMatcher.load(tmp_path)
    assert not matcher.is_ignored(".env", is_directory=False)
    assert not matcher.is_ignored(".hidden", is_directory=True)


def test_load_preserves_escaped_whitespace(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("name\\ \n", encoding="utf-8")
    matcher = GitIgnoreMatcher.load(tmp_path)
    assert matcher.is_ignored("name ", is_directory=False)


def test_unreadable_root_ignore_rules_are_not_treated_as_missing(tmp_path: Path) -> None:
    from voidcode.capabilities.tool_errors import ToolDiagnosticError

    (tmp_path / ".gitignore").write_bytes(b"\xff")
    with pytest.raises(ToolDiagnosticError, match="Cannot read ignore rules"):
        GitIgnoreMatcher.load(tmp_path)


def test_unreadable_nested_ignore_rules_fail_when_the_directory_is_reached(tmp_path: Path) -> None:
    from voidcode.capabilities.tool_errors import ToolDiagnosticError

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_bytes(b"\xff")
    matcher = GitIgnoreMatcher.load(tmp_path)
    assert not matcher.is_ignored("other", is_directory=False)
    with pytest.raises(ToolDiagnosticError, match="Cannot read ignore rules"):
        matcher.is_ignored("sub/other", is_directory=False)
