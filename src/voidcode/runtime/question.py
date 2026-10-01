from __future__ import annotations

from dataclasses import dataclass, field

from ..core.questions import PendingQuestionPrompt


@dataclass(frozen=True, slots=True)
class PendingQuestion:
    request_id: str
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    prompts: tuple[PendingQuestionPrompt, ...] = ()
