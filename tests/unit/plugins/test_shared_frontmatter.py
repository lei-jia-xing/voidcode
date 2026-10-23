"""Shared YAML framing retains validation while agents and skills share one parser."""

import pytest

from voidcode.capabilities.frontmatter import MAX_FRONTMATTER_CHARS, FrontmatterError, load_frontmatter_mapping, split_frontmatter


def test_crlf_delimiters_quoted_hashes_and_yaml_types():
    raw, body = split_frontmatter('---  \r\nname: "a # b"\r\nflag: true\r\nitems: [one, two]\r\n---\r\n body \r\n')
    assert body == "body"
    assert load_frontmatter_mapping(raw) == {"name": "a # b", "flag": True, "items": ["one", "two"]}


@pytest.mark.parametrize(
    "raw,match",
    [
        ("name: one\nname: two", "duplicate frontmatter key"),
        ("nested:\n  key: one\n  key: two", "duplicate frontmatter key"),
        ("1: value", "must be a string"),
        ("key: [", "invalid YAML frontmatter on line"),
        ("[one, two]", "must be a mapping"),
        ("!!python/object:os.system {}", "invalid YAML"),
        ("a" * (MAX_FRONTMATTER_CHARS + 1), "must not exceed"),
    ],
)
def test_bad_metadata_is_not_silently_accepted(raw, match):
    with pytest.raises(FrontmatterError, match=match):
        load_frontmatter_mapping(raw)


def test_unterminated_framing_and_domain_body_requirement():
    with pytest.raises(FrontmatterError, match="must close"):
        split_frontmatter("---\nname: one")
    assert split_frontmatter("---\n---\n") == ("", "")
    with pytest.raises(FrontmatterError, match="empty markdown body"):
        split_frontmatter("---\n---\n", require_body=True)
