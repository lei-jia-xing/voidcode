from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.tools import GlobTool, ToolCall

CI_SETTINGS = settings(derandomize=True, database=None, max_examples=100)

_name_chars = st.characters(min_codepoint=97, max_codepoint=122)
_base_name = st.text(alphabet=_name_chars, min_size=1, max_size=8)
_limit = 100


@CI_SETTINGS
@given(file_names=st.lists(_base_name, min_size=1, max_size=12, unique=True))
def test_glob_tool_txt_matches_equal_reported_count(file_names: list[str]) -> None:
    with TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        expected_matches: list[str] = []
        for index, name in enumerate(file_names):
            suffix = ".txt" if index % 2 == 0 else ".py"
            path = workspace / f"{name}{suffix}"
            path.write_text(name, encoding="utf-8")
            if suffix == ".txt":
                expected_matches.append(path.relative_to(workspace).as_posix())

        tool = GlobTool()
        result = tool.invoke(
            ToolCall(tool_name="glob", arguments={"pattern": "*.txt"}), workspace=workspace
        )

        matches = cast(list[str], result.data["matches"])

        assert result.status == "ok"
        assert sorted(matches) == sorted(expected_matches)
        assert result.data["count"] == len(matches)


@CI_SETTINGS
@given(file_names=st.lists(_base_name, min_size=1, max_size=8, unique=True))
def test_glob_tool_ignores_default_ignored_directories(file_names: list[str]) -> None:
    with TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        ignored_dir = workspace / "node_modules"
        ignored_dir.mkdir()
        visible_dir = workspace / "visible"
        visible_dir.mkdir()

        expected_matches: list[str] = []
        for name in file_names:
            ignored_file = ignored_dir / f"{name}.js"
            ignored_file.write_text(name, encoding="utf-8")

            visible_file = visible_dir / f"{name}.js"
            visible_file.write_text(name, encoding="utf-8")
            expected_matches.append(visible_file.relative_to(workspace).as_posix())

        tool = GlobTool()
        result = tool.invoke(
            ToolCall(tool_name="glob", arguments={"pattern": "**/*.js"}),
            workspace=workspace,
        )

        matches = cast(list[str], result.data["matches"])

        assert result.status == "ok"
        assert sorted(matches) == sorted(expected_matches)
        assert all("node_modules" not in match for match in matches)


def test_glob_tool_reports_truncation_at_limit(tmp_path: Path) -> None:
    for index in range(_limit + 5):
        (tmp_path / f"file_{index}.txt").write_text(str(index), encoding="utf-8")

    tool = GlobTool()
    result = tool.invoke(
        ToolCall(tool_name="glob", arguments={"pattern": "*.txt"}), workspace=tmp_path
    )

    matches = cast(list[str], result.data["matches"])

    assert result.status == "ok"
    assert result.data["truncated"] is True
    assert len(matches) >= _limit
    assert result.data["count"] == len(matches)
    assert "Results are truncated" in (result.content or "")
