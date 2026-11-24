"""Unit tests for the Btw plugin: bounds, cancellation, typed failures, and no staging."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.btw import create_plugin
from voidcode.plugins.btw.btw_plugin import (
    ANSWER_TRUNCATION_MARKER,
    MAX_ANSWER_BYTES,
    MAX_OUTPUT_TOKENS,
    MAX_QUESTION_BYTES,
    MAX_QUESTION_CHARS,
    BtwCommand,
    BtwErrorCode,
    clip_answer,
    wrap_side_question,
)
from voidcode.sdk.commands import CommandInvocation, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.provider import ProviderExecutionError
from voidcode.sdk.services import (
    ModelQueryBudgetExhausted,
    ModelQueryCancelled,
    ModelQueryContextFailed,
    ModelQueryModelUnavailable,
    ModelQueryPromptTooLong,
    ModelQueryToolCalls,
    ModelQueryUnavailable,
)


class _Abort:
    """Minimal ProviderAbortSignal for handler-level cancellation tests."""

    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return "test cancel" if self._cancelled else None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        _ = reason
        self._cancelled = value


class _RecordingServices(FakeServices):
    """FakeServices that records the model-query output cap requested by the plugin."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.output_caps: list[int | None] = []

    def model_query(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> str:
        self.output_caps.append(max_output_tokens)
        return super().model_query(
            prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            include_context=include_context,
        )


def _context(workspace: Path, abort_signal: _Abort | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="user",
        logical_turn="turn-1",
        abort_signal=abort_signal,
    )


def _invoke(
    services: FakeServices,
    tmp_path: Path,
    arguments: Mapping[str, object],
    *,
    abort_signal: _Abort | None = None,
) -> CommandResult:
    if services.answer is None and services.model_answer is None and services.failure is None:
        services.answer = "because the sky scatters blue light"
    return BtwCommand().invoke(
        CommandInvocation(context=_context(tmp_path, abort_signal), name="btw", arguments=arguments),
        services,
    )


def test_wrapper_keeps_the_side_turn_constraints_and_question_verbatim() -> None:
    wrapped = wrap_side_question("why is the sky blue?")
    assert wrapped.startswith("This is a side question from the user.")
    assert "You have NO tools available" in wrapped
    assert "there will be no follow-up turns" in wrapped
    assert wrapped.endswith("\n\nwhy is the sky blue?")


def test_question_passthrough_and_answer_returned(tmp_path: Path) -> None:
    services = FakeServices(answer="Rayleigh scattering.")
    result = _invoke(services, tmp_path, {"question": "why is the sky blue?"})
    assert result.status == "ok"
    assert result.output == "Rayleigh scattering."
    assert result.error is None
    assert result.data == {"question": "why is the sky blue?", "truncated": False}
    assert services.prompts == [wrap_side_question("why is the sky blue?")]


def test_question_is_stripped_before_the_provider_turn(tmp_path: Path) -> None:
    services = FakeServices()
    result = _invoke(services, tmp_path, {"question": "   spaces around   "})
    assert result.status == "ok"
    assert services.prompts == [wrap_side_question("spaces around")]
    assert result.data == {"question": "spaces around", "truncated": False}


def test_empty_or_missing_question_is_an_error_without_a_provider_turn(tmp_path: Path) -> None:
    for arguments in ({}, {"question": ""}, {"question": "   "}, {"question": 7}):
        services = FakeServices()
        result = _invoke(services, tmp_path, arguments)
        assert result.status == "error"
        assert result.error == "side question must not be empty"
        assert result.output is None
        assert result.data == {"error": BtwErrorCode.EMPTY_QUESTION.value}
        assert services.prompts == []


def test_model_query_requests_the_bounded_output_cap(tmp_path: Path) -> None:
    services = _RecordingServices()
    result = _invoke(services, tmp_path, {"question": "how much?"})
    assert result.status == "ok"
    assert services.output_caps == [MAX_OUTPUT_TOKENS]


def test_question_over_the_prompt_byte_budget_is_refused_without_a_provider_turn(tmp_path: Path) -> None:
    services = FakeServices()
    question = "😀" * (MAX_QUESTION_BYTES // 4 + 1)
    result = _invoke(services, tmp_path, {"question": question})
    assert result.status == "error"
    assert result.error == "side question is too long for the 64 KiB prompt limit"
    assert result.data == {"error": BtwErrorCode.QUESTION_TOO_LONG.value}
    assert services.prompts == []


def test_empty_answer_is_a_typed_error_after_the_provider_turn(tmp_path: Path) -> None:
    for answer in ("", "   ", "\n"):
        services = FakeServices(answer=answer)
        result = _invoke(services, tmp_path, {"question": "anything?"})
        assert result.status == "error"
        assert result.error == "side question received an empty response"
        assert result.output is None
        assert result.data == {"error": BtwErrorCode.EMPTY_ANSWER.value}
        assert services.prompts == [wrap_side_question("anything?")]


def test_provider_failure_is_stable_and_does_not_echo_exception_text(tmp_path: Path) -> None:
    services = FakeServices(
        failure=ProviderExecutionError(
            kind="provider_error",
            provider_name="endpoint",
            model_name="model",
            message="provider response contained secret-hint",
        )
    )
    result = _invoke(services, tmp_path, {"question": "anything?"})
    assert result.status == "error"
    assert result.error == "side question failed: provider error"
    assert result.data == {"error": BtwErrorCode.PROVIDER.value}
    assert "secret-hint" not in str(result.as_payload())
    assert result.output is None


@pytest.mark.parametrize(
    ("error", "code"),
    [
        # Every message is deliberately misleading: classification is type-only.
        (ModelQueryBudgetExhausted("model query prompt too long"), BtwErrorCode.BUDGET),
        (ModelQueryContextFailed("no model configured"), BtwErrorCode.CONTEXT),
        (ModelQueryUnavailable("budget limit exceeded"), BtwErrorCode.UNAVAILABLE),
        (ModelQueryModelUnavailable("context provider failed"), BtwErrorCode.MODEL),
        (ModelQueryPromptTooLong("provider error"), BtwErrorCode.QUESTION_TOO_LONG),
        (ModelQueryCancelled("tool calls returned"), BtwErrorCode.CANCELLED),
        (ModelQueryToolCalls("budget limit exceeded"), BtwErrorCode.TOOL_CALL),
        (
            ProviderExecutionError(
                kind="cancelled",
                provider_name="endpoint",
                model_name="model",
                message="budget limit exceeded",
            ),
            BtwErrorCode.CANCELLED,
        ),
        (
            ProviderExecutionError(
                kind="provider_error",
                provider_name="endpoint",
                model_name="model",
                message="budget limit exceeded",
            ),
            BtwErrorCode.PROVIDER,
        ),
        (RuntimeError("budget limit exceeded"), BtwErrorCode.UNKNOWN),
        (ValueError("no model configured"), BtwErrorCode.UNKNOWN),
    ],
)
def test_failures_classify_by_type_not_message(tmp_path: Path, error: Exception, code: BtwErrorCode) -> None:
    services = FakeServices(failure=error)
    result = _invoke(services, tmp_path, {"question": "anything?"})
    assert result.status == "error"
    assert result.data == {"error": code.value}
    assert result.output is None


def test_tool_call_reply_is_a_typed_error(tmp_path: Path) -> None:
    services = FakeServices(failure=ModelQueryToolCalls("governed model query returned tool calls; none were offered"))
    result = _invoke(services, tmp_path, {"question": "anything?"})
    assert result.status == "error"
    assert result.error == "side question was answered with a tool call instead of text"
    assert result.data == {"error": BtwErrorCode.TOOL_CALL.value}
    assert "governed model query" not in str(result.as_payload())


def test_cancelled_query_is_a_typed_error_without_a_provider_turn(tmp_path: Path) -> None:
    services = FakeServices()
    result = _invoke(services, tmp_path, {"question": "anything?"}, abort_signal=_Abort(cancelled=True))
    assert result.status == "error"
    assert result.error == "side question was cancelled"
    assert result.data == {"error": BtwErrorCode.CANCELLED.value}
    assert services.prompts == []


def test_cancelled_model_query_is_a_typed_error(tmp_path: Path) -> None:
    services = FakeServices(failure=ModelQueryCancelled("model query cancelled"))
    result = _invoke(services, tmp_path, {"question": "anything?"})
    assert result.status == "error"
    assert result.error == "side question was cancelled"
    assert result.data == {"error": BtwErrorCode.CANCELLED.value}


def test_long_answer_is_clipped_and_the_answer_is_stored_once(tmp_path: Path) -> None:
    services = FakeServices(answer="x" * (MAX_ANSWER_BYTES + 100))
    result = _invoke(services, tmp_path, {"question": "anything?"})
    assert result.status == "ok"
    assert result.output is not None
    assert result.output.endswith(ANSWER_TRUNCATION_MARKER)
    assert len(result.output.encode("utf-8")) <= MAX_ANSWER_BYTES
    assert result.data == {"question": "anything?", "truncated": True}
    assert "answer" not in result.data


def test_clip_answer_never_splits_a_utf8_code_point() -> None:
    clipped, truncated = clip_answer("😀" * (MAX_ANSWER_BYTES // 4 + 10))
    assert truncated is True
    clipped.encode("utf-8")  # must stay encodable
    assert clipped.endswith(ANSWER_TRUNCATION_MARKER)


def test_no_state_or_events_are_staged(tmp_path: Path) -> None:
    services = FakeServices()
    result = _invoke(services, tmp_path, {"question": "status?"})
    assert result.status == "ok"
    assert len(services.prompts) == 1
    assert services.staged_events == []
    assert services.staged_continuations == []
    assert services.states == {}
    assert services.row_counts == {}


def test_create_plugin_registers_the_btw_command_with_its_bounds() -> None:
    plugin = create_plugin(None)
    commands = [command.definition for command in plugin.commands]
    assert [command.name for command in commands] == ["btw"]
    definition = commands[0]
    assert definition.description == "ask a quick side question without interrupting the main conversation"
    assert definition.argument_schema["required"] == ("question",)
    assert definition.argument_schema["additionalProperties"] is False
    question_schema = definition.argument_schema["properties"]["question"]
    assert question_schema["maxLength"] == MAX_QUESTION_CHARS
    assert definition.effects == frozenset({"network"})
    assert definition.busy_gate is False
    assert plugin.tools == ()
    assert plugin.context is None
    assert plugin.observer is None
