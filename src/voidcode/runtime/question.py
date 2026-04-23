from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PendingQuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class PendingQuestionPrompt:
    question: str
    header: str
    options: tuple[PendingQuestionOption, ...] = ()
    multiple: bool = False


@dataclass(frozen=True, slots=True)
class PendingQuestion:
    request_id: str
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    prompts: tuple[PendingQuestionPrompt, ...] = ()


@dataclass(frozen=True, slots=True)
class QuestionResponse:
    header: str
    answers: tuple[str, ...]
