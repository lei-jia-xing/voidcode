"""Answer a tool-free side question using the current conversation window."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import ClassVar, Final

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderAbortSignal, ProviderExecutionError
from voidcode.sdk.services import (
    ModelQueryBudgetExhausted,
    ModelQueryCancelled,
    ModelQueryContextFailed,
    ModelQueryModelUnavailable,
    ModelQueryPromptTooLong,
    ModelQueryToolCalls,
    ModelQueryUnavailable,
    PluginServices,
)
from voidcode.sdk.tools import ToolEffect

#: The side turn has context, but cannot invoke tools or continue autonomously.
SIDE_QUESTION_WRAPPER = (
    "This is a side question from the user. Answer it directly in a single response.\n"
    "You have NO tools available and there will be no follow-up turns; answer only from the conversation context.\n"
    "If you do not know the answer, say so — do not offer to look it up.\n\n"
)

#: Core-enforced prompt ceiling for one governed model query.
MAX_PROMPT_BYTES: Final[int] = 64 * 1024
#: The question may use whatever room the wrapper leaves in that prompt.
MAX_QUESTION_BYTES: Final[int] = MAX_PROMPT_BYTES - len(SIDE_QUESTION_WRAPPER.encode("utf-8"))
#: JSON Schema counts code points, so this is an upper bound; the handler enforces bytes.
MAX_QUESTION_CHARS: Final[int] = MAX_QUESTION_BYTES
#: One side answer is deliberately short; the provider cap and byte clip are both enforced.
MAX_OUTPUT_TOKENS: Final[int] = 2048
MAX_ANSWER_BYTES: Final[int] = 16 * 1024
ANSWER_TRUNCATION_MARKER: Final[str] = "\n[answer truncated by /btw]"


class BtwErrorCode(StrEnum):
    """Stable command error codes; no raw provider exception text crosses the seam."""

    EMPTY_QUESTION = "empty_question"
    QUESTION_TOO_LONG = "question_too_long"
    CANCELLED = "cancelled"
    EMPTY_ANSWER = "empty_answer"
    TOOL_CALL = "tool_call"
    BUDGET = "budget"
    MODEL = "model"
    CONTEXT = "context"
    UNAVAILABLE = "unavailable"
    PROVIDER = "provider_error"
    UNKNOWN = "failed"


_ERROR_MESSAGES: Final[dict[BtwErrorCode, str]] = {
    BtwErrorCode.EMPTY_QUESTION: "side question must not be empty",
    BtwErrorCode.QUESTION_TOO_LONG: "side question is too long for the 64 KiB prompt limit",
    BtwErrorCode.CANCELLED: "side question was cancelled",
    BtwErrorCode.EMPTY_ANSWER: "side question received an empty response",
    BtwErrorCode.TOOL_CALL: "side question was answered with a tool call instead of text",
    BtwErrorCode.BUDGET: "side question blocked by an active budget",
    BtwErrorCode.MODEL: "side question is unavailable: no configured model",
    BtwErrorCode.CONTEXT: "side question failed: conversation context is unavailable",
    BtwErrorCode.UNAVAILABLE: "side question is unavailable",
    BtwErrorCode.PROVIDER: "side question failed: provider error",
    BtwErrorCode.UNKNOWN: "side question failed",
}


def wrap_side_question(question: str) -> str:
    """Wrap the cleaned question with the constraints of a side turn."""
    return f"{SIDE_QUESTION_WRAPPER}{question}"


def clip_answer(answer: str) -> tuple[str, bool]:
    """Bound one side answer by UTF-8 bytes without splitting a code point."""
    encoded = answer.encode("utf-8")
    if len(encoded) <= MAX_ANSWER_BYTES:
        return answer, False
    keep = MAX_ANSWER_BYTES - len(ANSWER_TRUNCATION_MARKER.encode("utf-8"))
    clipped = encoded[:keep].decode("utf-8", errors="ignore").rstrip()
    return f"{clipped}{ANSWER_TRUNCATION_MARKER}", True


def _is_cancelled(signal: ProviderAbortSignal | None) -> bool:
    return signal is not None and signal.cancelled


def _error(code: BtwErrorCode) -> CommandResult:
    return CommandResult(status="error", error=_ERROR_MESSAGES[code], data={"error": code.value})


def _failure_code(exc: Exception, abort_signal: ProviderAbortSignal | None) -> BtwErrorCode:
    """Map every failure to a stable code using types and structured fields only."""
    if _is_cancelled(abort_signal) or isinstance(exc, ModelQueryCancelled):
        return BtwErrorCode.CANCELLED
    if isinstance(exc, ModelQueryToolCalls):
        return BtwErrorCode.TOOL_CALL
    if isinstance(exc, ModelQueryPromptTooLong):
        return BtwErrorCode.QUESTION_TOO_LONG
    if isinstance(exc, ModelQueryBudgetExhausted):
        return BtwErrorCode.BUDGET
    if isinstance(exc, ModelQueryContextFailed):
        return BtwErrorCode.CONTEXT
    if isinstance(exc, ModelQueryUnavailable):
        return BtwErrorCode.UNAVAILABLE
    if isinstance(exc, ModelQueryModelUnavailable):
        return BtwErrorCode.MODEL
    if isinstance(exc, ProviderExecutionError):
        return BtwErrorCode.CANCELLED if exc.kind == "cancelled" else BtwErrorCode.PROVIDER
    return BtwErrorCode.UNKNOWN


class BtwCommand:
    """Answer one side question through ``services.model_query``."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="btw",
        description="ask a quick side question without interrupting the main conversation",
        argument_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_QUESTION_CHARS,
                    "description": "The side question to answer from the conversation context.",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        # One provider turn: no plugin state or workspace mutation, so a read-only session keeps it.
        effects=frozenset({ToolEffect.NETWORK}),
        # A side question may be asked while the session is busy.
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw_question = invocation.arguments.get("question")
        if not isinstance(raw_question, str) or not raw_question.strip():
            return _error(BtwErrorCode.EMPTY_QUESTION)
        question = raw_question.strip()
        if len(question.encode("utf-8")) > MAX_QUESTION_BYTES:
            return _error(BtwErrorCode.QUESTION_TOO_LONG)
        abort_signal = invocation.context.abort_signal
        if _is_cancelled(abort_signal):
            return _error(BtwErrorCode.CANCELLED)
        try:
            answer = services.model_query(
                wrap_side_question(question),
                max_output_tokens=MAX_OUTPUT_TOKENS,
                include_context=True,
            )
        except Exception as exc:  # noqa: BLE001 - classify, never echo provider text
            return _error(_failure_code(exc, abort_signal))
        if _is_cancelled(abort_signal):
            return _error(BtwErrorCode.CANCELLED)
        if not answer.strip():
            return _error(BtwErrorCode.EMPTY_ANSWER)
        clipped, truncated = clip_answer(answer)
        return CommandResult(
            status="ok",
            output=clipped,
            data={"question": question, "truncated": truncated},
        )


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(BtwCommand(),))
