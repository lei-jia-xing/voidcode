from __future__ import annotations

from dataclasses import dataclass


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
    allow_freeform: bool = False


@dataclass(frozen=True, slots=True)
class QuestionResponse:
    header: str
    answers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuestionCancellation:
    """Cancel the complete pending question request."""


type QuestionAnswer = tuple[QuestionResponse, ...] | QuestionCancellation
