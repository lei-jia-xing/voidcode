from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.tools import GrepTool, ToolCall
from voidcode.tools.contracts import ToolResult

CI_SETTINGS = settings(derandomize=True, database=None, max_examples=200)

_text_chars = st.characters(
    min_codepoint=32,
    max_codepoint=126,
    blacklist_characters=["\n", "\r"],
)
_single_line = st.text(alphabet=_text_chars, min_size=0, max_size=16)
_multiline_text = st.lists(_single_line, min_size=0, max_size=12).map("\n".join)
_pattern_chars = st.characters(
    min_codepoint=32,
    max_codepoint=126,
    blacklist_characters=["\n", "\r"],
)
_pattern = st.text(alphabet=_pattern_chars, min_size=1, max_size=6).filter(
    lambda value: bool(value.strip())
)


def _invoke_grep(*, content: str, pattern: str) -> tuple[ToolResult, Path]:
    with TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        sample_file = workspace / "sample.txt"
        sample_file.write_text(content, encoding="utf-8")
        result = GrepTool().invoke(
            ToolCall(tool_name="grep", arguments={"pattern": pattern, "path": "sample.txt"}),
            workspace=workspace,
        )
        return result, sample_file


def _expected_matches(*, content: str, pattern: str) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for line_number, line_text in enumerate(content.splitlines(), start=1):
        columns: list[int] = []
        start_index = 0

        while True:
            found_index = line_text.find(pattern, start_index)
            if found_index < 0:
                break
            columns.append(found_index + 1)
            start_index = found_index + len(pattern)

        if columns:
            matches.append(
                {
                    "file": "sample.txt",
                    "line": line_number,
                    "text": line_text,
                    "columns": columns,
                    "before": [],
                    "after": [],
                }
            )

    return matches


@CI_SETTINGS
@given(content=_multiline_text, pattern=_pattern)
def test_grep_tool_match_count_equals_total_reported_columns(content: str, pattern: str) -> None:
    result, _ = _invoke_grep(content=content, pattern=pattern)

    matches = cast(list[dict[str, object]], result.data["matches"])

    assert result.status == "ok"
    assert result.data["pattern"] == pattern
    assert result.data["path"] == "sample.txt"
    assert result.data["regex"] is False
    assert result.data["context"] == 0
    assert result.data["match_count"] == sum(
        len(cast(list[int], match["columns"])) for match in matches
    )


@CI_SETTINGS
@given(content=_multiline_text, pattern=_pattern)
def test_grep_tool_reports_exact_literal_line_and_column_matches(
    content: str, pattern: str
) -> None:
    result, _ = _invoke_grep(content=content, pattern=pattern)

    expected_matches = _expected_matches(content=content, pattern=pattern)

    assert result.data["matches"] == expected_matches
    assert result.data["match_count"] == sum(
        len(cast(list[int], match["columns"])) for match in expected_matches
    )


@CI_SETTINGS
@given(content=_multiline_text, pattern=_pattern)
def test_grep_tool_summary_matches_preview_contract(content: str, pattern: str) -> None:
    result, _ = _invoke_grep(content=content, pattern=pattern)

    expected_matches = _expected_matches(content=content, pattern=pattern)
    match_count = sum(len(cast(list[int], match["columns"])) for match in expected_matches)

    if expected_matches:
        preview_lines = [
            f"sample.txt:{match['line']}: {match['text']}" for match in expected_matches[:10]
        ]
        expected_summary = (
            f"Found {match_count} match(es) for {pattern!r} in sample.txt\n"
            + "\n".join(preview_lines)
        )
    else:
        expected_summary = f"Found 0 match(es) for {pattern!r} in sample.txt"

    assert result.content == expected_summary
