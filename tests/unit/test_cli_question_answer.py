from __future__ import annotations

import pytest
from pydantic import ValidationError

from voidcode.cli.commands.sessions import parse_question_responses
from voidcode.cli.errors import CliError
from voidcode.core.questions import QuestionCancellation
from voidcode.runtime.transport.http_contract import _QuestionAnswerRequestPayload


def test_cli_cancel_uses_the_question_cancellation_union() -> None:
    assert parse_question_responses(cancel=True) == QuestionCancellation()


def test_cli_cancel_cannot_be_combined_with_answers() -> None:
    with pytest.raises(CliError, match="--cancel cannot be used"):
        parse_question_responses(response=("answer",), cancel=True)
    with pytest.raises(CliError, match="--cancel cannot be used"):
        parse_question_responses(response_json='[{"header":"Name","answers":["answer"]}]', cancel=True)


def test_http_question_answer_cancel_has_one_unambiguous_request_shape() -> None:
    cancel = _QuestionAnswerRequestPayload.model_validate({"request_id": "q1", "cancel": True})
    assert cancel.cancel is True
    assert cancel.responses is None

    with pytest.raises(ValidationError):
        _QuestionAnswerRequestPayload.model_validate({"request_id": "q1", "cancel": True, "responses": [{"header": "Name", "answers": ["x"]}]})
    with pytest.raises(ValidationError):
        _QuestionAnswerRequestPayload.model_validate({"request_id": "q1", "responses": None})
