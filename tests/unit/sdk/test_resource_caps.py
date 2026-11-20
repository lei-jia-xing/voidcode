"""Resource capability discovery, enumeration/search shapes, and reader slots."""

from __future__ import annotations

import pytest

from voidcode.sdk.resources import (
    ResourceEnumEntry,
    ResourceEnumPage,
    ResourceEnumRequest,
    ResourceReader,
    ResourceSchemeCaps,
    ResourceSearchMatch,
    ResourceSearchPage,
    ResourceSearchRequest,
    ResourceUnsupported,
)


def test_caps_require_detail_unconditionally() -> None:
    caps = ResourceSchemeCaps(
        scheme="proc",
        enumerable=True,
        searchable=False,
        line_paging=True,
        byte_paging=True,
        inventory_uri="proc://",
        detail="root pages by line, leaves by byte",
    )
    assert caps.inventory_uri == "proc://"
    # The proc hole: line == byte still needs a detail note; no boolean mix rescues it.
    with pytest.raises(ValueError, match="detail"):
        ResourceSchemeCaps(scheme="proc", enumerable=True, searchable=False, line_paging=True, byte_paging=True)
    with pytest.raises(ValueError, match="detail"):
        ResourceSchemeCaps(scheme="proc", enumerable=True, searchable=False, line_paging=True, byte_paging=True, detail="  ")
    with pytest.raises(ValueError, match="detail"):
        ResourceSchemeCaps(scheme="tool", enumerable=False, searchable=False, line_paging=True, byte_paging=False, detail="")


def test_caps_allow_missing_inventory_for_multi_root() -> None:
    caps = ResourceSchemeCaps(scheme="tool", enumerable=False, searchable=False, line_paging=True, byte_paging=False, detail="roots in detail")
    assert caps.inventory_uri is None


def test_reader_defaults_and_slot_validation() -> None:
    def handler(request, caller, services):  # type: ignore[no-untyped-def]
        return None

    reader = ResourceReader(scheme="proc", handler=handler)
    assert reader.capabilities is None and reader.enumerate is None and reader.search is None
    caps = ResourceSchemeCaps(scheme="proc", enumerable=True, searchable=False, line_paging=True, byte_paging=True, detail="d")
    full = ResourceReader(scheme="proc", handler=handler, capabilities=caps, enumerate=handler, search=handler)  # type: ignore[arg-type]
    assert full.capabilities == caps
    with pytest.raises(ValueError, match="capabilities"):
        ResourceReader(scheme="proc", handler=handler, capabilities="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="enumerate"):
        ResourceReader(scheme="proc", handler=handler, enumerate="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="search"):
        ResourceReader(scheme="proc", handler=handler, search="nope")  # type: ignore[arg-type]


def test_enum_request_cursor_validation_and_scheme_lowering() -> None:
    assert ResourceEnumRequest(uri="PROC://").scheme == "proc"
    with pytest.raises(ValueError, match="offset"):
        ResourceEnumRequest(uri="proc://", offset=0)
    with pytest.raises(ValueError, match="limit"):
        ResourceEnumRequest(uri="proc://", limit=0)
    with pytest.raises(ValueError, match="URI"):
        ResourceEnumRequest(uri="no-scheme")
    with pytest.raises(ValueError, match="URI"):
        ResourceEnumRequest(uri="")


def test_enum_entry_and_page_validation() -> None:
    entry = ResourceEnumEntry(uri="proc://abc", label="job")
    assert entry.label == "job"
    with pytest.raises(ValueError, match="URI"):
        ResourceEnumEntry(uri="relative/path")
    page = ResourceEnumPage(entries=(entry,), next_offset=2, total_count=9)
    assert page.total_count == 9
    assert ResourceEnumPage(entries=(), next_offset=None).total_count is None
    with pytest.raises(ValueError, match="next_offset"):
        ResourceEnumPage(entries=(), next_offset=0)
    with pytest.raises(ValueError, match="total_count"):
        ResourceEnumPage(entries=(), next_offset=None, total_count=-1)
    with pytest.raises(ValueError, match="entries"):
        ResourceEnumPage(entries=("nope",), next_offset=None)  # type: ignore[arg-type]


def test_search_request_validation_and_scheme_lowering() -> None:
    request = ResourceSearchRequest(uri="PROC://logs", pattern="err.*", regex=True, ignore_case=True, offset=2, limit=50)
    assert request.scheme == "proc"
    with pytest.raises(ValueError, match="pattern"):
        ResourceSearchRequest(uri="proc://", pattern="")
    with pytest.raises(ValueError, match="regex"):
        ResourceSearchRequest(uri="proc://", pattern="x", regex=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="ignore_case"):
        ResourceSearchRequest(uri="proc://", pattern="x", ignore_case="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="offset"):
        ResourceSearchRequest(uri="proc://", pattern="x", offset=0)
    with pytest.raises(ValueError, match="URI"):
        ResourceSearchRequest(uri="bare", pattern="x")


def test_search_match_and_page_validation() -> None:
    match = ResourceSearchMatch(uri="proc://a", text="boom", line=12)
    assert match.line == 12 and match.byte_offset is None
    assert ResourceSearchMatch(uri="proc://a", text="", byte_offset=0).text == ""
    with pytest.raises(ValueError, match="line"):
        ResourceSearchMatch(uri="proc://a", text="x", line=0)
    with pytest.raises(ValueError, match="byte_offset"):
        ResourceSearchMatch(uri="proc://a", text="x", byte_offset=-1)
    with pytest.raises(ValueError, match="text"):
        ResourceSearchMatch(uri="proc://a", text=5)  # type: ignore[arg-type]
    page = ResourceSearchPage(matches=(match,), next_offset=None, truncated=True)
    assert page.truncated is True
    with pytest.raises(ValueError, match="truncated"):
        ResourceSearchPage(matches=(), next_offset=None, truncated=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="matches"):
        ResourceSearchPage(matches=("nope",), next_offset=None)  # type: ignore[arg-type]


def test_resource_unsupported_is_plain_value() -> None:
    assert issubclass(ResourceUnsupported, ValueError)
