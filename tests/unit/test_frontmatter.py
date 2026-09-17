from __future__ import annotations

from datetime import date

import pytest

from voidcode.frontmatter import (
    MAX_FRONTMATTER_CHARS,
    FrontmatterError,
    load_frontmatter_mapping,
    split_frontmatter,
)


def test_split_frontmatter_returns_raw_block_and_trimmed_body() -> None:
    raw, body = split_frontmatter("---\nname: demo\n---\n\n# Body\n")

    assert raw == "name: demo\n"
    assert body == "# Body"


def test_split_frontmatter_tolerates_delimiter_padding_and_crlf() -> None:
    raw, body = split_frontmatter("---\r\nname: demo\r\n  ---  \r\nBody\r\n")

    assert raw == "name: demo\r\n"
    assert body == "Body"


def test_split_frontmatter_does_not_truncate_on_delimiter_prefixed_value() -> None:
    raw, body = split_frontmatter("---\nname: demo\nnote: ---not-a-delimiter\n---\nBody\n")

    assert raw is not None
    assert "---not-a-delimiter" in raw
    assert body == "Body"


def test_split_frontmatter_requires_delimiter_by_default() -> None:
    with pytest.raises(FrontmatterError, match=r"^demo\.md: must start with a YAML frontmatter delimiter"):
        _ = split_frontmatter("# plain markdown\n", source="demo.md")


def test_split_frontmatter_allows_missing_delimiter_when_not_required() -> None:
    assert split_frontmatter("# plain markdown\n", require_delimiter=False, require_closing=False) == (None, "# plain markdown\n")


def test_split_frontmatter_requires_closing_delimiter_by_default() -> None:
    with pytest.raises(FrontmatterError, match=r"^demo\.md: must close the YAML frontmatter block"):
        _ = split_frontmatter("---\nname: demo\n", source="demo.md")


def test_split_frontmatter_treats_unterminated_block_as_plain_text_when_closing_not_required() -> None:
    text = "---\nname: demo\n"

    assert split_frontmatter(text, require_delimiter=False, require_closing=False) == (None, text)


def test_split_frontmatter_requires_non_empty_body_when_requested() -> None:
    with pytest.raises(FrontmatterError, match="must not have an empty markdown body"):
        _ = split_frontmatter("---\nname: demo\n---\n\n", require_body=True)


def test_split_frontmatter_returns_verbatim_body_when_not_stripping() -> None:
    raw, body = split_frontmatter("---\nname: demo\n---\n\nBody\n\n", strip_body=False)

    assert raw == "name: demo\n"
    assert body == "\nBody\n\n"


def test_load_frontmatter_mapping_returns_empty_mapping_without_content() -> None:
    assert load_frontmatter_mapping(None) == {}
    assert load_frontmatter_mapping("") == {}
    assert load_frontmatter_mapping("\n# only a comment\n") == {}


def test_load_frontmatter_mapping_supports_flow_sequence_with_quoted_comma() -> None:
    loaded = load_frontmatter_mapping('tool_allowlist: [read, "grep, ripgrep"]\n')

    assert loaded == {"tool_allowlist": ["read", "grep, ripgrep"]}


def test_load_frontmatter_mapping_supports_flow_mapping_and_nested_block_mapping() -> None:
    loaded = load_frontmatter_mapping("flow: {profile: docs, servers: [repo, context7]}\nblock:\n  profile: docs\n  servers:\n    - repo\n")

    assert loaded == {
        "flow": {"profile": "docs", "servers": ["repo", "context7"]},
        "block": {"profile": "docs", "servers": ["repo"]},
    }


def test_load_frontmatter_mapping_supports_literal_and_folded_block_scalars() -> None:
    loaded = load_frontmatter_mapping("literal: |\n  one\n  two\nfolded: >\n  one\n  two\n")

    assert loaded == {"literal": "one\ntwo\n", "folded": "one two\n"}


def test_load_frontmatter_mapping_applies_standard_yaml_implicit_typing() -> None:
    loaded = load_frontmatter_mapping("flag: yes\ndate: 2024-01-01\ncount: 3\nratio: 1.5\nquoted: 'yes'\n")

    assert loaded == {
        "flag": True,
        "date": date(2024, 1, 1),
        "count": 3,
        "ratio": 1.5,
        "quoted": "yes",
    }


def test_load_frontmatter_mapping_treats_unquoted_hash_as_comment() -> None:
    assert load_frontmatter_mapping("name: demo # trailing note\n") == {"name": "demo"}


def test_load_frontmatter_mapping_rejects_duplicate_keys() -> None:
    with pytest.raises(FrontmatterError, match=r"^demo\.md: duplicate frontmatter key 'name' on line 2"):
        _ = load_frontmatter_mapping("name: one\nname: two\n", source="demo.md")


def test_load_frontmatter_mapping_rejects_duplicate_nested_keys() -> None:
    with pytest.raises(FrontmatterError, match="duplicate frontmatter key 'profile' on line 3"):
        _ = load_frontmatter_mapping("mcp_binding:\n  profile: docs\n  profile: other\n")


def test_load_frontmatter_mapping_allows_merge_key_override() -> None:
    loaded = load_frontmatter_mapping("base: &base\n  a: 1\nderived:\n  <<: *base\n  a: 2\n")

    assert loaded["derived"] == {"a": 2}


def test_load_frontmatter_mapping_rejects_non_string_keys() -> None:
    with pytest.raises(FrontmatterError, match="frontmatter key 'on' on line 1 must be a string"):
        _ = load_frontmatter_mapping("on: true\n")


def test_load_frontmatter_mapping_rejects_non_mapping_document() -> None:
    with pytest.raises(FrontmatterError, match="frontmatter must be a mapping, not list"):
        _ = load_frontmatter_mapping("- one\n- two\n")


def test_load_frontmatter_mapping_reports_yaml_syntax_errors_with_position() -> None:
    with pytest.raises(FrontmatterError, match=r"^demo\.md: invalid YAML frontmatter on line 2, column"):
        _ = load_frontmatter_mapping("name: demo\nbroken: a: b\n", source="demo.md")


def test_load_frontmatter_mapping_rejects_oversized_frontmatter() -> None:
    raw = "name: " + "x" * MAX_FRONTMATTER_CHARS

    with pytest.raises(FrontmatterError, match=rf"^demo\.md: frontmatter must not exceed {MAX_FRONTMATTER_CHARS} characters"):
        _ = load_frontmatter_mapping(raw, source="demo.md")


def test_load_frontmatter_mapping_never_constructs_python_objects() -> None:
    with pytest.raises(FrontmatterError, match="invalid YAML frontmatter"):
        _ = load_frontmatter_mapping("value: !!python/object/apply:os.system ['true']\n")
