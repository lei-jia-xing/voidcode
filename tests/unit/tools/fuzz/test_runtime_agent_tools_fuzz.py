from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.runtime.contracts import BackgroundTaskResult
from voidcode.runtime.task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
)
from voidcode.skills.models import SkillMetadata
from voidcode.tools import (
    BackgroundCancelTool,
    BackgroundOutputTool,
    QuestionTool,
    SkillTool,
    TaskTool,
    ToolCall,
)

CI_SETTINGS = settings(derandomize=True, database=None, deadline=None, max_examples=200)

_text_chars = st.characters(
    blacklist_categories=["Cs"],
    blacklist_characters=["\x00", "\n", "\r"],
)
_non_blank_text = st.text(alphabet=_text_chars, min_size=1, max_size=20).filter(
    lambda text: text.strip() != "" and text == text.strip()
)
_blank_text = st.sampled_from(("", " ", "  ", "\t", " \t "))
_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
)
_json_like = st.recursive(
    _json_scalar | _non_blank_text,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(_non_blank_text, children, max_size=3)
    ),
    max_leaves=6,
)
_invalid_text_value = st.one_of(_blank_text, _json_scalar, st.lists(_json_like, max_size=3))


class _RecordingBackgroundOutputRuntime:
    def __init__(self) -> None:
        self.task_ids: list[str] = []

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        self.task_ids.append(task_id)
        return BackgroundTaskResult(
            task_id=task_id,
            parent_session_id="leader-session",
            child_session_id=None,
            status="completed",
            summary_output="done",
            result_available=True,
        )

    def session_result(self, *, session_id: str) -> object:
        raise AssertionError(session_id)


class _RecordingBackgroundCancelRuntime:
    def __init__(self) -> None:
        self.task_ids: list[str] = []

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        self.task_ids.append(task_id)
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=task_id),
            status="cancelled",
            request=BackgroundTaskRequestSnapshot(prompt="delegated"),
        )


class _UnusedTaskRuntime:
    def run(self, request: object) -> object:
        raise AssertionError(request)

    def start_background_task(self, request: object) -> object:
        raise AssertionError(request)

    def load_background_task_result(self, task_id: str) -> object:
        raise AssertionError(task_id)

    def cancel_background_task(self, task_id: str) -> object:
        raise AssertionError(task_id)

    def list_background_tasks(self) -> tuple[object, ...]:
        raise AssertionError("list_background_tasks should not be called")

    def session_result(self, *, session_id: str) -> object:
        raise AssertionError(session_id)


@CI_SETTINGS
@given(
    question_text=_non_blank_text,
    header=_non_blank_text,
    option_labels=st.lists(_non_blank_text, min_size=1, max_size=4),
    multiple=st.booleans(),
)
def test_question_tool_parse_prompts_trims_valid_payloads(
    question_text: str,
    header: str,
    option_labels: list[str],
    multiple: bool,
) -> None:
    prompts = QuestionTool.parse_prompts(
        {
            "questions": [
                {
                    "question": f"  {question_text}  ",
                    "header": f"  {header}  ",
                    "options": [{"label": f"  {label}  "} for label in option_labels],
                    "multiple": multiple,
                }
            ]
        }
    )

    assert len(prompts) == 1
    assert prompts[0].question == question_text
    assert prompts[0].header == header
    assert prompts[0].multiple is multiple
    assert [option.label for option in prompts[0].options] == option_labels


@CI_SETTINGS
@given(
    bad_questions=st.one_of(
        _json_scalar,
        st.just([]),
        st.lists(st.one_of(_json_scalar, _blank_text), min_size=1, max_size=4),
    )
)
def test_question_tool_parse_prompts_rejects_malformed_questions_payload(
    bad_questions: object,
) -> None:
    with pytest.raises(ValueError, match="question requires a non-empty questions array"):
        QuestionTool.parse_prompts({"questions": bad_questions})


@CI_SETTINGS
@given(task_id=_non_blank_text)
def test_background_output_tool_trims_task_id_before_runtime_lookup(task_id: str) -> None:
    runtime = _RecordingBackgroundOutputRuntime()
    tool = BackgroundOutputTool(runtime=runtime)

    with TemporaryDirectory() as temp_dir:
        result = tool.invoke(
            ToolCall(tool_name="background_output", arguments={"task_id": f"  {task_id}  "}),
            workspace=Path(temp_dir),
        )

    assert result.status == "ok"
    assert runtime.task_ids == [task_id]
    assert result.data["task_id"] == task_id


@CI_SETTINGS
@given(task_id=_invalid_text_value)
def test_background_output_tool_rejects_invalid_task_id_values(task_id: object) -> None:
    tool = BackgroundOutputTool(runtime=_RecordingBackgroundOutputRuntime())

    with TemporaryDirectory() as temp_dir:
        with pytest.raises(ValueError, match="background_output requires a non-empty task_id"):
            tool.invoke(
                ToolCall(tool_name="background_output", arguments={"task_id": task_id}),
                workspace=Path(temp_dir),
            )


@CI_SETTINGS
@given(task_id=_non_blank_text)
def test_background_cancel_tool_trims_task_id_before_cancelling(task_id: str) -> None:
    runtime = _RecordingBackgroundCancelRuntime()
    tool = BackgroundCancelTool(runtime=runtime)

    with TemporaryDirectory() as temp_dir:
        result = tool.invoke(
            ToolCall(tool_name="background_cancel", arguments={"taskId": f"  {task_id}  "}),
            workspace=Path(temp_dir),
        )

    assert result.status == "ok"
    assert runtime.task_ids == [task_id]
    assert result.data["task_id"] == task_id


@CI_SETTINGS
@given(task_id=_invalid_text_value)
def test_background_cancel_tool_rejects_invalid_task_id_values(task_id: object) -> None:
    tool = BackgroundCancelTool(runtime=_RecordingBackgroundCancelRuntime())

    with TemporaryDirectory() as temp_dir:
        with pytest.raises(ValueError):
            tool.invoke(
                ToolCall(tool_name="background_cancel", arguments={"taskId": task_id}),
                workspace=Path(temp_dir),
            )


@CI_SETTINGS
@given(name=_invalid_text_value)
def test_skill_tool_rejects_invalid_name_values(name: object) -> None:
    tool = SkillTool(
        list_skills=lambda: (),
        resolve_skill=lambda skill_name: SkillMetadata(
            name=skill_name,
            description="demo",
            directory=Path("/tmp/skills/demo"),
            entry_path=Path("/tmp/skills/demo/SKILL.md"),
            content="# Demo",
        ),
    )

    with TemporaryDirectory() as temp_dir:
        with pytest.raises(ValueError, match="skill requires a non-empty string name"):
            tool.invoke(
                ToolCall(tool_name="skill", arguments={"name": name}),
                workspace=Path(temp_dir),
            )


@CI_SETTINGS
@given(prompt=_invalid_text_value, run_in_background=st.booleans())
def test_task_tool_rejects_invalid_prompt_values(
    prompt: object,
    run_in_background: bool,
) -> None:
    tool = TaskTool(runtime=_UnusedTaskRuntime())

    with TemporaryDirectory() as temp_dir:
        with pytest.raises(
            ValueError,
            match=(
                "task requires prompt, run_in_background, load_skills, and exactly one "
                "of category or subagent_type"
            ),
        ):
            tool.invoke(
                ToolCall(
                    tool_name="task",
                    arguments={
                        "prompt": prompt,
                        "run_in_background": run_in_background,
                        "load_skills": [],
                        "category": "quick",
                    },
                ),
                workspace=Path(temp_dir),
            )


@CI_SETTINGS
@given(skill_name=_invalid_text_value, run_in_background=st.booleans())
def test_task_tool_rejects_invalid_load_skills_entries(
    skill_name: object,
    run_in_background: bool,
) -> None:
    tool = TaskTool(runtime=_UnusedTaskRuntime())

    with TemporaryDirectory() as temp_dir:
        with pytest.raises(
            ValueError,
            match=(
                "task requires prompt, run_in_background, load_skills, and exactly one "
                "of category or subagent_type"
            ),
        ):
            tool.invoke(
                ToolCall(
                    tool_name="task",
                    arguments={
                        "prompt": "Investigate this",
                        "run_in_background": run_in_background,
                        "load_skills": [skill_name],
                        "category": "quick",
                    },
                ),
                workspace=Path(temp_dir),
            )


def test_task_tool_rejects_ambiguous_or_missing_routing_arguments() -> None:
    tool = TaskTool(runtime=_UnusedTaskRuntime())

    with TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        with pytest.raises(ValueError, match="task requires prompt"):
            tool.invoke(
                ToolCall(
                    tool_name="task",
                    arguments={
                        "prompt": "Investigate this",
                        "run_in_background": True,
                        "load_skills": [],
                    },
                ),
                workspace=workspace,
            )
        with pytest.raises(ValueError, match="task requires prompt"):
            tool.invoke(
                ToolCall(
                    tool_name="task",
                    arguments={
                        "prompt": "Investigate this",
                        "run_in_background": True,
                        "load_skills": [],
                        "category": "quick",
                        "subagent_type": "explore",
                    },
                ),
                workspace=workspace,
            )
