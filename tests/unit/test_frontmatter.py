from __future__ import annotations

import pytest

from voidcode.frontmatter import (
    FrontmatterError,
    load_frontmatter_mapping,
    split_frontmatter,
)


def test_split_frontmatter_returns_raw_block_and_trimmed_body() -> None:
    raw, body = split_frontmatter("---\nname: demo\n---\n\n# Body\n")

    assert raw == "name: demo\n"
    assert body == "# Body"


def test_split_frontmatter_requires_closing_delimiter_by_default() -> None:
    with pytest.raises(FrontmatterError, match=r"^demo\.md: must close the YAML frontmatter block"):
        _ = split_frontmatter("---\nname: demo\n", source="demo.md")


def test_split_frontmatter_requires_non_empty_body_when_requested() -> None:
    with pytest.raises(FrontmatterError, match="must not have an empty markdown body"):
        _ = split_frontmatter("---\nname: demo\n---\n\n", require_body=True)


def test_load_frontmatter_mapping_supports_literal_and_folded_block_scalars() -> None:
    loaded = load_frontmatter_mapping("literal: |\n  one\n  two\nfolded: >\n  one\n  two\n")

    assert loaded == {"literal": "one\ntwo\n", "folded": "one two\n"}


def test_load_frontmatter_mapping_rejects_duplicate_keys() -> None:
    with pytest.raises(FrontmatterError, match=r"^demo\.md: duplicate frontmatter key 'name' on line 2"):
        _ = load_frontmatter_mapping("name: one\nname: two\n", source="demo.md")


def test_load_frontmatter_mapping_reports_yaml_syntax_errors_with_position() -> None:
    with pytest.raises(FrontmatterError, match=r"^demo\.md: invalid YAML frontmatter on line 2, column"):
        _ = load_frontmatter_mapping("name: demo\nbroken: a: b\n", source="demo.md")
