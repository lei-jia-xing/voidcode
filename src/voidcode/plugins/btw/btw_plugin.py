"""Answer a tool-free side question using the current conversation window."""

from __future__ import annotations

from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

#: The side turn has context, but cannot invoke tools or continue autonomously.
SIDE_QUESTION_WRAPPER = (
    "This is a side question from the user. Answer it directly in a single response.\n"
    "You have NO tools available and there will be no follow-up turns; answer only from the conversation context.\n"
    "If you do not know the answer, say so — do not offer to look it up.\n\n"
)


def wrap_side_question(question: str) -> str:
    """Wrap the cleaned question with the constraints of a side turn."""
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
        # A side question may be asked while the session is busy.
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw_question = invocation.arguments.get("question")
        if not isinstance(raw_question, str) or not raw_question.strip():
            return CommandResult(status="error", error="side question must not be empty")
        question = raw_question.strip()
        try:
            answer = services.model_query(wrap_side_question(question), include_context=True)
        except Exception as exc:  # noqa: BLE001 - the failure itself is what the user must see
            return CommandResult(status="error", error=f"side question failed: {type(exc).__name__}: {exc}")
        if not answer.strip():
            return CommandResult(status="error", error="side question received an empty response")
        return CommandResult(status="ok", output=answer, data={"question": question, "answer": answer})
