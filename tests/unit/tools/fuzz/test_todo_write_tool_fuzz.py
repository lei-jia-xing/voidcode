from __future__ import annotations

import importlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, cast

from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.tools import TodoWriteTool, ToolCall

_todo_write = importlib.import_module("voidcode.tools.todo_write")


class _ParseTodoItemFn(Protocol):
    def __call__(self, item: object, *, idx: int) -> dict[str, str]: ...


_parse_todo_item = cast(_ParseTodoItemFn, _todo_write._parse_todo_item)

CI_SETTINGS = settings(derandomize=True, database=None, max_examples=200)

_content_chars = st.characters(
    blacklist_characters=["\x00", "\n", "\r", "\t", "\x0b", "\x0c"],
    blacklist_categories=["Cs"],
)
_content_text = st.text(alphabet=_content_chars, min_size=1, max_size=30).filter(
    lambda text: text.strip() != ""
)
_status = st.sampled_from(("pending", "in_progress", "completed", "cancelled"))
_priority = st.sampled_from(("high", "medium", "low"))
_todo_item = st.fixed_dictionaries(
    {
        "content": _content_text.map(lambda text: f"  {text}  "),
        "status": _status,
        "priority": _priority,
    }
)


@CI_SETTINGS
@given(item=_todo_item, idx=st.integers(min_value=1, max_value=50))
def test_parse_todo_item_trims_content_and_preserves_enums(item: dict[str, str], idx: int) -> None:
    parsed = _parse_todo_item(item, idx=idx)

    assert parsed["content"] == item["content"].strip()
    assert parsed["status"] == item["status"]
    assert parsed["priority"] == item["priority"]


@CI_SETTINGS
@given(todos=st.lists(_todo_item, min_size=0, max_size=12))
def test_todo_write_summary_matches_normalized_status_counts(todos: list[dict[str, str]]) -> None:
    tool = TodoWriteTool()

    with TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        result = tool.invoke(
            ToolCall(tool_name="todo_write", arguments={"todos": todos}),
            workspace=workspace,
        )

        stored = cast(list[dict[str, str]], result.data["todos"])
        summary = cast(dict[str, int], result.data["summary"])

        assert result.status == "ok"
        assert not (workspace / ".voidcode" / "todos.json").exists()
        assert summary["total"] == len(stored)
        assert summary["pending"] == sum(1 for item in stored if item["status"] == "pending")
        assert summary["in_progress"] == sum(
            1 for item in stored if item["status"] == "in_progress"
        )
        assert summary["completed"] == sum(1 for item in stored if item["status"] == "completed")
        assert summary["cancelled"] == sum(1 for item in stored if item["status"] == "cancelled")
        assert all(item["content"] == item["content"].strip() for item in stored)
