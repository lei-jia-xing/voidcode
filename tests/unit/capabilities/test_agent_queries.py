"""Shared named-agent query contract: absence, mismatch, and effective restrictions."""

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from voidcode.capabilities.agent_queries import (
    AGENT_DEFINITIONS,
    AgentDefinition,
    AgentDefinitionRequest,
    apply_agent_profile,
)
from voidcode.sdk.plugin import PluginQueryCaller
from voidcode.sdk.services import QueryContractError, QueryUnavailable

_FIXTURE = AgentDefinition(
    name="review",
    description="Delegated review of evidence, risks, and options.",
    prompt="Review.",
    source_scope="builtin",
    source_path=None,
    readonly=True,
    max_turns=4,
)


class Transport:
    def __init__(self, answer: object) -> None:
        self.answer = answer

    def query(self, name: str, arguments: object) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_unknown_agent_is_none_while_unavailable_and_broken_are_visible() -> None:
    request = AgentDefinitionRequest(name="missing")
    assert AGENT_DEFINITIONS.ask(cast(Any, Transport(None)), request) is None
    with pytest.raises(QueryUnavailable):
        AGENT_DEFINITIONS.ask(cast(Any, Transport(QueryUnavailable("no implementation"))), request)
    with pytest.raises(RuntimeError):
        AGENT_DEFINITIONS.ask(cast(Any, Transport(RuntimeError("provider bug"))), request)


def test_answer_for_another_agent_is_malformed() -> None:
    with pytest.raises(QueryContractError):
        AGENT_DEFINITIONS.ask(cast(Any, Transport(_FIXTURE.as_payload())), AgentDefinitionRequest(name="other"))


def test_extra_answer_key_is_malformed() -> None:
    answer = {**_FIXTURE.as_payload(), "model": "inherit"}
    with pytest.raises(QueryContractError):
        AGENT_DEFINITIONS.ask(cast(Any, Transport(answer)), AgentDefinitionRequest(name=_FIXTURE.name))


def test_bound_query_rejects_bad_request_before_handler() -> None:
    def handler(_request: AgentDefinitionRequest, _caller: PluginQueryCaller) -> AgentDefinition:
        pytest.fail("invalid request reached handler")

    caller = PluginQueryCaller(session_id="session", workspace=Path("/workspace"))
    for arguments in ({"name": ""}, {"name": 3}, {"name": "x", "extra": 1}):
        with pytest.raises(ValidationError):
            AGENT_DEFINITIONS.bind(handler).handler(arguments, caller)


def test_apply_agent_profile_only_narrows_an_assigned_budget() -> None:
    _, profile_readonly, profile_tools, profile_max_steps = apply_agent_profile(_FIXTURE, "look", ("read", "write"), 32)
    assert (profile_readonly, profile_tools, profile_max_steps) == (True, ("read", "write"), 4)
    _, _, _, unassigned_steps = apply_agent_profile(_FIXTURE, "look", None, None)
    assert unassigned_steps is None
