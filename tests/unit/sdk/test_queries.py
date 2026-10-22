"""Typed queries preserve transport failures and reject malformed peer data."""

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from voidcode.sdk.plugin import PluginQueryCaller
from voidcode.sdk.queries import QueryContractError
from voidcode.sdk.services import QueryUnavailable
from voidcode.sdk.workspace_queries import DOCUMENT_STATES, RULE_PAGES, DocumentStateRequest, RulePage, RulePageRequest


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


@pytest.mark.parametrize("arguments", [{"uri": "  "}, {"uri": "rule", "offset": True}, {"uri": "rule", "limit": 2001}, {"uri": "rule", "extra": 1}])
def test_bound_query_rejects_invalid_requests_before_handler(arguments: dict[str, object]) -> None:
    def handler(_request: RulePageRequest, _caller: PluginQueryCaller) -> RulePage:
        pytest.fail("invalid request reached handler")

    with pytest.raises(ValidationError):
        RULE_PAGES.bind(handler).handler(arguments, PluginQueryCaller(session_id="session", workspace=Path("/workspace")))


def test_query_can_bind_an_independent_implementation() -> None:
    caller = PluginQueryCaller(session_id="session", workspace=Path("/workspace"))

    def handler(request: RulePageRequest, actual: PluginQueryCaller) -> RulePage:
        assert actual == caller
        assert request.offset == 1 and request.limit == 2000
        return RulePage("style", "a" * 64, "Independent provider", None)

    query = RULE_PAGES.bind(handler)
    assert query.name == RULE_PAGES.name
    answer = query.handler({"uri": "voidcode://rule/style"}, caller)
    page = RULE_PAGES.ask(cast(Any, Transport(answer)), RulePageRequest(uri="voidcode://rule/style"))
    assert page is not None and page.text == "Independent provider"
