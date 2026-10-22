"""Shared optional workspace query answers preserve unknown and invalid states."""

import pytest

from voidcode.sdk.workspace_queries import DocumentState, RulePage


@pytest.mark.parametrize("version", [True, False, 0, -1, "1", None])
def test_document_state_rejects_invalid_versions(version: object) -> None:
    answer = {"path": "/workspace/a.py", "version": version, "content_hash": "a" * 64, "position_encoding": "utf-16"}
    assert DocumentState.from_query(answer, path="/workspace/a.py") is None


def test_document_state_rejects_wrong_document_or_hash() -> None:
    state = DocumentState("/workspace/a.py", 1, "a" * 64, "utf-16")
    assert DocumentState.from_query(state.as_payload(), path=state.path) == state
    assert DocumentState.from_query(state.as_payload(), path="/workspace/b.py") is None
    assert DocumentState.from_query({**state.as_payload(), "content_hash": "unknown"}, path=state.path) is None
    assert DocumentState.from_query({**state.as_payload(), "position_encoding": "unknown"}, path=state.path) is None


@pytest.mark.parametrize("offset", [True, False, 0, -1, "2"])
def test_rule_page_rejects_invalid_pagination(offset: object) -> None:
    page = RulePage("style", "a" * 64, "Use tabs", None)
    assert RulePage.from_query({**page.as_payload(), "next_offset": offset}) is None


def test_rule_page_preserves_terminal_page_and_next_offset() -> None:
    for offset in (None, 2):
        page = RulePage("style", "a" * 64, "", offset)
        assert RulePage.from_query(page.as_payload()) == page
