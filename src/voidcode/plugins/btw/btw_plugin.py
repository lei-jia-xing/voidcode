"""Btw plugin: answer a side question with one governed provider turn.

Ported from legacy ``RuntimeService.ask_side_question`` (native ``/btw``): the answer
comes from one tool-free, history-free provider turn, so the session transcript is never
touched — no history pollution, no tools, and no follow-up turn. The kernel records the
canonical ``kernel.model_query`` diagnostic fact for the call, so this plugin stages no
state and no events of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

#: Legacy wrapper instruction, verbatim (``runtime/service.py:ask_side_question``).
SIDE_QUESTION_WRAPPER = (
    "This is a side question from the user. Answer it directly in a single response.\n"
    "You have NO tools available and there will be no follow-up turns; answer only from the conversation context.\n"
    "If you do not know the answer, say so — do not offer to look it up.\n\n"
)


def wrap_side_question(question: str) -> str:
    """The exact prompt the legacy fork sent: the wrapper plus the cleaned question."""
    return f"{SIDE_QUESTION_WRAPPER}{question}"


class BtwCommand:
    """Answer one side question through ``services.model_query``."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="btw",
        description="ask a quick side question without interrupting the main conversation",
        argument_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1, "description": "The side question to answer from the conversation context."},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        # One provider turn: no session or workspace mutation, so a read-only session keeps it.
        effects=frozenset({ToolEffect.NETWORK}),
        # Legacy parity: a side question may be asked while the session is busy.
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw_question = invocation.arguments.get("question")
        if not isinstance(raw_question, str) or not raw_question.strip():
            return CommandResult(status="error", error="side question must not be empty")
        question = raw_question.strip()
        try:
            answer = services.model_query(wrap_side_question(question))
        except Exception as exc:  # noqa: BLE001 - the failure itself is what the user must see
            return CommandResult(status="error", error=f"side question failed: {type(exc).__name__}: {exc}")
        if not answer.strip():
            return CommandResult(status="error", error="side question received an empty response")
        return CommandResult(status="ok", output=answer, data={"question": question, "answer": answer})


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(BtwCommand(),))
