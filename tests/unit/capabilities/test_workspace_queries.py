"""Shared optional workspace query contracts and transport boundary tests."""

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from voidcode.capabilities.workspace_queries import (
    DOCUMENT_STATES,
    RULE_PAGES,
    DocumentState,
    DocumentStateRequest,
    RulePage,
    RulePageRequest,
)
from voidcode.sdk.plugin import PluginQueryCaller
from voidcode.sdk.queries import QueryContractError
from voidcode.sdk.services import QueryUnavailable


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


class Transport:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def query(self, name: str, arguments: object) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_optional_query_distinguishes_absence_resource_missing_and_handler_failure() -> None:
    request = RulePageRequest(uri="voidcode://rule/example")
    assert RULE_PAGES.ask(cast(Any, Transport(None)), request) is None
    for failure in (QueryUnavailable("no implementation"), RuntimeError("provider bug")):
        with pytest.raises(type(failure)) as caught:
            RULE_PAGES.ask(cast(Any, Transport(failure)), request)
        assert caught.value is failure


@pytest.mark.parametrize("answer", [[], "bad", {}, {"rule": "style", "text": "body"}])
def test_query_rejects_malformed_answers(answer: object) -> None:
    with pytest.raises(QueryContractError):
        RULE_PAGES.ask(cast(Any, Transport(answer)), RulePageRequest(uri="voidcode://rule/style"))


def test_document_answer_must_belong_to_requested_document() -> None:
    answer = {"path": "/other", "version": 1, "content_hash": "a" * 64, "position_encoding": "utf-16"}
    with pytest.raises(QueryContractError):
        DOCUMENT_STATES.ask(cast(Any, Transport(answer)), DocumentStateRequest(path="/requested"))


@pytest.mark.parametrize(
    "arguments",
    [
        {"uri": "  "},
        {"uri": "rule", "offset": True},
        {"uri": "rule", "limit": 2001},
        {"uri": "rule", "extra": 1},
    ],
)
def test_bound_query_rejects_invalid_requests_before_handler(arguments: dict[str, object]) -> None:
    def handler(_request: RulePageRequest, _caller: PluginQueryCaller) -> RulePage:
        pytest.fail("invalid request reached handler")

    with pytest.raises(ValidationError):
        RULE_PAGES.bind(handler).handler(arguments, PluginQueryCaller(session_id="session", workspace=Path("/workspace")))
