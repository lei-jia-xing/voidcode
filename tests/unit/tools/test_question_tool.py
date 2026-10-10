from __future__ import annotations

import pytest

from voidcode.core.questions import PendingQuestionOption, PendingQuestionPrompt, QuestionResponse
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


def test_freeform_question_without_options_accepts_user_answers() -> None:
    prompts = QuestionTool.parse_prompts(
        {
            "questions": [
                {
                    "question": "What should we call this?",
                    "header": "Name",
                    "options": [],
                    "allow_freeform": True,
                }
            ]
        }
    )

    assert prompts[0].allow_freeform is True
    assert QuestionTool.validate_responses(
        prompts,
        (QuestionResponse(header="Name", answers=("a user-provided value",)),),
    ) == (QuestionResponse(header="Name", answers=("a user-provided value",)),)


def test_prompt_options_and_headers_are_validated_before_preparing() -> None:
    with pytest.raises(ValueError, match="option labels must be unique"):
        QuestionTool.parse_prompts(
            {
                "questions": [
                    {
                        "question": "Choose one",
                        "header": "Choice",
                        "options": [{"label": "A"}, {"label": " A "}],
                    }
                ]
            }
        )

    with pytest.raises(ValueError, match="question headers must be unique"):
        QuestionTool.parse_prompts(
            {
                "questions": [
                    {"question": "First", "header": "Choice", "options": [{"label": "A"}]},
                    {"question": "Second", "header": " Choice ", "options": [{"label": "B"}]},
                ]
            }
        )


def test_single_choice_cardinality_and_declared_options_are_enforced() -> None:
    prompts = QuestionTool.parse_prompts({"questions": [{"question": "Choose one", "header": "Choice", "options": [{"label": "A"}, {"label": "B"}]}]})

    with pytest.raises(ValueError, match="accepts exactly one answer"):
        QuestionTool.validate_responses(
            prompts,
            (QuestionResponse(header="Choice", answers=("A", "B")),),
        )
    with pytest.raises(ValueError, match="match one of the declared option labels"):
        QuestionTool.validate_responses(
            prompts,
            (QuestionResponse(header="Choice", answers=("C",)),),
        )
