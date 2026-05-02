from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ValidationError, field_validator

from ..runtime.question import PendingQuestionOption, PendingQuestionPrompt, QuestionResponse
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult


class _QuestionOptionModel(BaseModel):
    label: str
    description: str = ""

    @field_validator("label", mode="after")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("option label must be non-empty")
        return stripped


class _QuestionPromptModel(BaseModel):
    question: str
    header: str
    options: list[_QuestionOptionModel]
    multiple: bool = False

    @field_validator("question", "header", mode="after")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question fields must be non-empty strings")
        return stripped

    @field_validator("options", mode="after")
    @classmethod
    def _validate_options(cls, value: list[_QuestionOptionModel]) -> list[_QuestionOptionModel]:
        if not value:
            raise ValueError("question options must not be empty")
        return value


class _QuestionArgsModel(BaseModel):
    questions: list[_QuestionPromptModel]

    @field_validator("questions", mode="after")
    @classmethod
    def _validate_questions(cls, value: list[_QuestionPromptModel]) -> list[_QuestionPromptModel]:
        if not value:
            raise ValueError("questions must not be empty")
        return value


class QuestionTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="question",
        description="Ask the user clarifying questions and wait for an answer.",
        input_schema={
            "questions": {
                "type": "array",
                "description": "Array of {question, header, options, multiple} objects.",
            }
        },
        read_only=True,
    )

    @staticmethod
    def parse_prompts(arguments: dict[str, object]) -> tuple[PendingQuestionPrompt, ...]:
        try:
            parsed = _QuestionArgsModel.model_validate(arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error("question", exc)) from exc
        return tuple(
            PendingQuestionPrompt(
                question=item.question,
                header=item.header,
                options=tuple(
                    PendingQuestionOption(label=option.label, description=option.description)
                    for option in item.options
                ),
                multiple=item.multiple,
            )
            for item in parsed.questions
        )

    @staticmethod
    def validate_responses(
        prompts: tuple[PendingQuestionPrompt, ...],
        responses: tuple[QuestionResponse, ...],
    ) -> tuple[QuestionResponse, ...]:
        prompt_by_header: dict[str, PendingQuestionPrompt] = {}
        for prompt in prompts:
            if prompt.header in prompt_by_header:
                raise ValueError(f"duplicate question header: {prompt.header}")
            prompt_by_header[prompt.header] = prompt
        response_by_header: dict[str, QuestionResponse] = {}
        for response in responses:
            if response.header in response_by_header:
                raise ValueError(f"duplicate question header: {response.header}")
            prompt = prompt_by_header.get(response.header)
            if prompt is None:
                raise ValueError(f"unknown question header: {response.header}")
            if not response.answers:
                raise ValueError(f"question '{response.header}' requires at least one answer")
            if not prompt.multiple and len(response.answers) != 1:
                raise ValueError(f"question '{response.header}' accepts exactly one answer")
            option_labels = {option.label for option in prompt.options}
            for answer in response.answers:
                if answer not in option_labels:
                    raise ValueError(
                        "question "
                        f"'{response.header}' answer must match one of the declared option labels"
                    )
            response_by_header[response.header] = response
        missing_headers = [
            prompt.header for prompt in prompts if prompt.header not in response_by_header
        ]
        if missing_headers:
            raise ValueError("missing answers for question headers: " + ", ".join(missing_headers))
        return tuple(response_by_header[prompt.header] for prompt in prompts)

    @staticmethod
    def answer_tool_result(responses: tuple[QuestionResponse, ...]) -> ToolResult:
        lines: list[str] = []
        payload: list[dict[str, object]] = []
        for response in responses:
            lines.append(f"{response.header}: {', '.join(response.answers)}")
            payload.append({"header": response.header, "answers": list(response.answers)})
        return ToolResult(
            tool_name=QuestionTool.definition.name,
            status="ok",
            content="\n".join(lines),
            data={"responses": payload},
        )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        prompts = self.parse_prompts(call.arguments)
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Prepared {len(prompts)} question(s)",
            data={
                "questions": [
                    {
                        "question": prompt.question,
                        "header": prompt.header,
                        "options": [
                            {"label": option.label, "description": option.description}
                            for option in prompt.options
                        ],
                        "multiple": prompt.multiple,
                    }
                    for prompt in prompts
                ]
            },
        )
