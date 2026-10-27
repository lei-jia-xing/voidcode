"""Shared optional workspace query contracts and transport boundary tests."""

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from voidcode.capabilities.workspace_queries import (
    DOCUMENT_STATES,
    DocumentState,
    DocumentStateRequest,
)
from voidcode.sdk.plugin import PluginQueryCaller
from voidcode.sdk.services import QueryContractError, QueryUnavailable


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


class Transport:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def query(self, name: str, arguments: object) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_optional_query_distinguishes_absence_resource_missing_and_handler_failure() -> None:
    request = DocumentStateRequest(path="/example")
    assert DOCUMENT_STATES.ask(cast(Any, Transport(None)), request) is None
    for failure in (QueryUnavailable("no implementation"), RuntimeError("provider bug")):
        with pytest.raises(type(failure)) as caught:
            DOCUMENT_STATES.ask(cast(Any, Transport(failure)), request)
        assert caught.value is failure


@pytest.mark.parametrize("answer", [[], "bad", {}, {"path": "/style"}])
def test_query_rejects_malformed_answers(answer: object) -> None:
    with pytest.raises(QueryContractError):
        DOCUMENT_STATES.ask(cast(Any, Transport(answer)), DocumentStateRequest(path="/style"))


def test_document_answer_must_belong_to_requested_document() -> None:
    answer = {"path": "/other", "version": 1, "content_hash": "a" * 64, "position_encoding": "utf-16"}
    with pytest.raises(QueryContractError):
        DOCUMENT_STATES.ask(cast(Any, Transport(answer)), DocumentStateRequest(path="/requested"))


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": ""},
        {"path": True},
        {"path": None},
        {"path": "/rule", "extra": 1},
    ],
)
def test_bound_query_rejects_invalid_requests_before_handler(arguments: dict[str, object]) -> None:
    def handler(_request: DocumentStateRequest, _caller: PluginQueryCaller) -> DocumentState:
        pytest.fail("invalid request reached handler")

    with pytest.raises(ValidationError):
        DOCUMENT_STATES.bind(handler).handler(arguments, PluginQueryCaller(session_id="session", workspace=Path("/workspace")))
