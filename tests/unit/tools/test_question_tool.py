from __future__ import annotations

import pytest

from voidcode.runtime.question import PendingQuestionOption, PendingQuestionPrompt, QuestionResponse
from voidcode.tools.question import QuestionTool


def test_validate_responses_rejects_duplicate_headers() -> None:
    prompts = (
        PendingQuestionPrompt(
            question="Which runtime path should we use?",
            header="Runtime path",
            options=(PendingQuestionOption(label="Reuse existing"),),
            multiple=False,
        ),
        PendingQuestionPrompt(
            question="Which review mode should we use?",
            header="Review mode",
            options=(PendingQuestionOption(label="Fast"),),
            multiple=False,
        ),
    )
    responses = (
        QuestionResponse(header="Runtime path", answers=("Reuse existing",)),
        QuestionResponse(header="Runtime path", answers=("Reuse existing",)),
    )

    with pytest.raises(ValueError, match="duplicate question header"):
        QuestionTool.validate_responses(prompts, responses)


def test_validate_responses_rejects_missing_headers() -> None:
    prompts = (
        PendingQuestionPrompt(
            question="Which runtime path should we use?",
            header="Runtime path",
            options=(PendingQuestionOption(label="Reuse existing"),),
            multiple=False,
        ),
        PendingQuestionPrompt(
            question="Which review mode should we use?",
            header="Review mode",
            options=(PendingQuestionOption(label="Fast"),),
            multiple=False,
        ),
    )
    responses = (QuestionResponse(header="Runtime path", answers=("Reuse existing",)),)

    with pytest.raises(ValueError, match="missing answers for question headers: Review mode"):
        QuestionTool.validate_responses(prompts, responses)


def test_validate_responses_returns_answers_in_prompt_order() -> None:
    prompts = (
        PendingQuestionPrompt(
            question="Which runtime path should we use?",
            header="Runtime path",
            options=(PendingQuestionOption(label="Reuse existing"),),
            multiple=False,
        ),
        PendingQuestionPrompt(
            question="Which review mode should we use?",
            header="Review mode",
            options=(PendingQuestionOption(label="Fast"),),
            multiple=False,
        ),
    )
    responses = (
        QuestionResponse(header="Review mode", answers=("Fast",)),
        QuestionResponse(header="Runtime path", answers=("Reuse existing",)),
    )

    normalized = QuestionTool.validate_responses(prompts, responses)

    assert normalized == (
        QuestionResponse(header="Runtime path", answers=("Reuse existing",)),
        QuestionResponse(header="Review mode", answers=("Fast",)),
    )
