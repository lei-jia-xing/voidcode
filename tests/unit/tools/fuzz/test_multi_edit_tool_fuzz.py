from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st

from voidcode.tools import MultiEditTool, ToolCall
from voidcode.tools.contracts import ToolResult

CI_SETTINGS = settings(derandomize=True, database=None, max_examples=200)

_safe_chars = st.characters(
    min_codepoint=97,
    max_codepoint=122,
    blacklist_categories=["Cs"],
)
_replacement_text = st.text(alphabet=_safe_chars, min_size=0, max_size=8)
_chain_step = st.text(alphabet=_safe_chars, min_size=1, max_size=8)
_replace_all_flag = st.booleans()


def _invoke_multi_edit(*, content: str, edits: list[dict[str, object]]) -> tuple[ToolResult, str]:
    with TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        sample_file = workspace / "sample.txt"
        sample_file.write_text(content, encoding="utf-8")

        result = MultiEditTool().invoke(
            ToolCall(tool_name="multi_edit", arguments={"path": "sample.txt", "edits": edits}),
            workspace=workspace,
        )

        return result, sample_file.read_text(encoding="utf-8")


@CI_SETTINGS
@given(
    replacements=st.lists(_replacement_text, min_size=1, max_size=5),
    replace_all=st.lists(_replace_all_flag, min_size=1, max_size=5),
)
def test_multi_edit_reports_applied_count_and_indexed_details_for_successful_edits(
    replacements: list[str], replace_all: list[bool]
) -> None:
    assume_count = min(len(replacements), len(replace_all))
    replacements = replacements[:assume_count]
    replace_all = replace_all[:assume_count]

    content_lines: list[str] = []
    edits: list[dict[str, object]] = []
    expected_content_parts: list[str] = []

    for index, replacement in enumerate(replacements, start=1):
        token = f"TOKEN_{index}"
        content_lines.append(f"before {token} after")
        edits.append(
            {
                "oldString": token,
                "newString": replacement,
                "replaceAll": replace_all[index - 1],
            }
        )
        expected_content_parts.append(f"before {replacement} after")

    result, final_content = _invoke_multi_edit(content="\n".join(content_lines), edits=edits)

    detail_entries = cast(list[dict[str, object]], result.data["edits"])

    assert result.status == "ok"
    assert result.content == f"Applied {len(edits)} edits to sample.txt"
    assert result.data["path"] == "sample.txt"
    assert result.data["applied"] == len(edits)
    assert len(detail_entries) == len(edits)
    assert [entry["index"] for entry in detail_entries] == list(range(1, len(edits) + 1))
    assert final_content == "\n".join(expected_content_parts)


@CI_SETTINGS
@given(steps=st.lists(_chain_step, min_size=2, max_size=5, unique=True))
def test_multi_edit_applies_edits_sequentially_against_updated_file_state(steps: list[str]) -> None:
    edits: list[dict[str, object]] = []
    current = "CHAIN_START"
    expected_content = current

    for next_value in steps:
        edits.append(
            {
                "oldString": current,
                "newString": next_value,
                "replaceAll": False,
            }
        )
        expected_content = expected_content.replace(current, next_value)
        current = next_value

    result, final_content = _invoke_multi_edit(content="CHAIN_START", edits=edits)

    assert result.status == "ok"
    assert result.data["applied"] == len(edits)
    assert final_content == expected_content


@CI_SETTINGS
@given(
    replacements=st.lists(_replacement_text, min_size=1, max_size=4),
    occurrence_counts=st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=4),
)
def test_multi_edit_replace_all_sequence_matches_python_replace_oracle(
    replacements: list[str], occurrence_counts: list[int]
) -> None:
    count = min(len(replacements), len(occurrence_counts))
    replacements = replacements[:count]
    occurrence_counts = occurrence_counts[:count]

    content_parts: list[str] = []
    edits: list[dict[str, object]] = []
    expected_content_parts: list[str] = []

    for index, replacement in enumerate(replacements, start=1):
        token = f"BLOCK_{index}"
        repeated = " ".join([token] * occurrence_counts[index - 1])
        content_parts.append(repeated)
        edits.append({"oldString": token, "newString": replacement, "replaceAll": True})
        expected_content_parts.append(repeated.replace(token, replacement))

    original_content = "\n".join(content_parts)
    expected_content = "\n".join(expected_content_parts)

    result, final_content = _invoke_multi_edit(content=original_content, edits=edits)

    assert result.status == "ok"
    assert result.data["applied"] == len(edits)
    assert final_content == expected_content
