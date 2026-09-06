from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.tools import TodoTool, ToolCall

CI_SETTINGS = settings(derandomize=True, database=None, max_examples=200)

_content_chars = st.characters(
    blacklist_characters=["\x00", "\n", "\r", "\t", "\x0b", "\x0c"],
    blacklist_categories=["Cs"],
)
_content_text = st.text(alphabet=_content_chars, min_size=1, max_size=30).filter(lambda text: text.strip() != "")


@CI_SETTINGS
@given(items=st.lists(_content_text, min_size=1, max_size=12, unique=True))
def test_todo_init_summary_matches_phase_tasks(items: list[str]) -> None:
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        result = TodoTool().invoke(
            ToolCall(tool_name="todo", arguments={"op": "init", "items": items}),
            workspace=tmp_path,
        )
        phases = result.data["phases"]
        summary = result.data["summary"]
        assert result.status == "ok"
        assert isinstance(phases, list)
        tasks = phases[0]["tasks"]
        assert summary["total"] == len(tasks) == len(items)
        assert summary["in_progress"] == 1
        assert summary["pending"] == len(items) - 1
        assert summary["completed"] == 0
        assert summary["abandoned"] == 0
        assert summary["blocked"] == 0
        assert not (tmp_path / ".voidcode" / "todos.json").exists()


@CI_SETTINGS
@given(prefix=_content_text, suffix=_content_text)
def test_todo_init_rejects_trim_collisions_and_blank(prefix: str, suffix: str) -> None:
    with TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        with pytest.raises(ValueError, match="Duplicate task"):
            TodoTool().invoke(
                ToolCall(tool_name="todo", arguments={"op": "init", "items": [prefix, f" {prefix} "]}),
                workspace=tmp_path,
            )
        with pytest.raises(ValueError, match="non-empty"):
            TodoTool().invoke(
                ToolCall(tool_name="todo", arguments={"op": "init", "items": [suffix, "   "]}),
                workspace=tmp_path,
            )
