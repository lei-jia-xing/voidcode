"""Unit tests for the Btw plugin: verbatim wrapper, question passthrough, visible failures."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.btw import create_plugin
from voidcode.plugins.btw.btw_plugin import (
    SIDE_QUESTION_WRAPPER,
    BtwCommand,
    wrap_side_question,
)
from voidcode.sdk.commands import CommandInvocation, CommandResult
from voidcode.sdk.invocation import ToolContext

#: Legacy wrapper instruction, copied verbatim from ``runtime/service.py:ask_side_question``.
LEGACY_WRAPPER = (
    "This is a side question from the user. Answer it directly in a single response.\n"
    "You have NO tools available and there will be no follow-up turns; answer only from the conversation context.\n"
    "If you do not know the answer, say so — do not offer to look it up.\n\n"
)


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="user",
        logical_turn="turn-1",
    )


def _invoke(services: FakeServices, tmp_path: Path, arguments: Mapping[str, object]) -> CommandResult:
    if services.answer is None and services.model_answer is None and services.failure is None:
        services.answer = "because the sky scatters blue light"
    return BtwCommand().invoke(CommandInvocation(context=_context(tmp_path), name="btw", arguments=arguments), services)


def test_wrapper_text_is_the_legacy_instruction_verbatim() -> None:
    assert SIDE_QUESTION_WRAPPER == LEGACY_WRAPPER
    assert wrap_side_question("why is the sky blue?") == LEGACY_WRAPPER + "why is the sky blue?"


def test_question_passthrough_and_answer_returned(tmp_path: Path) -> None:
    services = FakeServices(answer="Rayleigh scattering.")
    result = _invoke(services, tmp_path, {"question": "why is the sky blue?"})
    assert result.status == "ok"
    assert result.output == "Rayleigh scattering."
    assert result.error is None
    assert result.data == {"question": "why is the sky blue?", "answer": "Rayleigh scattering."}
    assert services.prompts == [LEGACY_WRAPPER + "why is the sky blue?"]


def test_question_is_stripped_before_the_provider_turn(tmp_path: Path) -> None:
    services = FakeServices()
    result = _invoke(services, tmp_path, {"question": "   spaces around   "})
    assert result.status == "ok"
    assert services.prompts == [LEGACY_WRAPPER + "spaces around"]
    assert result.data == {"question": "spaces around", "answer": services.answer}


def test_empty_or_missing_question_is_an_error_without_a_provider_turn(tmp_path: Path) -> None:
    for arguments in ({}, {"question": ""}, {"question": "   "}, {"question": 7}):
        services = FakeServices()
        result = _invoke(services, tmp_path, arguments)
        assert result.status == "error"
        assert result.error == "side question must not be empty"
        assert result.output is None
        assert services.prompts == []


def test_empty_answer_is_an_error(tmp_path: Path) -> None:
    for answer in ("", "   ", "\n"):
        services = FakeServices(answer=answer)
        result = _invoke(services, tmp_path, {"question": "anything?"})
        assert result.status == "error"
        assert result.error == "side question received an empty response"
        assert result.data is None
        assert services.prompts == [LEGACY_WRAPPER + "anything?"]


def test_provider_failure_is_an_error(tmp_path: Path) -> None:
    services = FakeServices(failure=RuntimeError("provider exploded"))
    result = _invoke(services, tmp_path, {"question": "anything?"})
    assert result.status == "error"
    assert result.error == "side question failed: RuntimeError: provider exploded"
    assert result.output is None


def test_no_state_or_events_are_staged(tmp_path: Path) -> None:
    # Every state/event seam on MockServices raises, so a green result proves the command
    # used exactly one provider turn and nothing else.
    services = FakeServices()
    result = _invoke(services, tmp_path, {"question": "status?"})
    assert result.status == "ok"
    assert len(services.prompts) == 1


def test_create_plugin_registers_the_btw_command() -> None:
    plugin = create_plugin(None)
    commands = [command.definition for command in plugin.commands]
    assert [command.name for command in commands] == ["btw"]
    definition = commands[0]
    assert definition.description == "ask a quick side question without interrupting the main conversation"
    assert definition.argument_schema["required"] == ("question",)
    assert definition.argument_schema["additionalProperties"] is False
    assert definition.effects == frozenset({"network"})
    assert definition.busy_gate is False
    assert plugin.tools == ()
    assert plugin.context is None
    assert plugin.observer is None
