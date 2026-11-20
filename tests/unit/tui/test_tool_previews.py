"""Tool presentation keeps selected text, failures, and source bounds honest."""

from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import EmptyOutput, OutputBounds, TextOutput, ToolFailure, ToolSuccess
from voidcode.tui.styles import Styles
from voidcode.tui.views import render_tool_call, render_tool_result


def test_arguments_have_a_bounded_preview_with_history_access() -> None:
    rows = render_tool_call("write", {"path": "a.py", "content": "content " * 100}, 40, Styles(None))
    assert len(rows) == 4
    assert '"path": "a.py"' in "\n".join(rows)
    assert "arguments shortened" in rows[-1]
    assert '/show {"events":true}' in rows[-1]


def test_success_preserves_the_selected_inline_text_without_business_line_caps() -> None:
    text = "\n".join(f"line {i}" for i in range(20))
    result = ToolSuccess("capture", TextOutput(text=text))
    rows = render_tool_result(result, 80, Styles(None))
    assert rows == text.splitlines()


def test_errors_are_not_shortened_and_terminal_controls_are_safe() -> None:
    detail = "\n".join(f"error {i}" for i in range(20)) + "\x1b[2J"
    rows = render_tool_result(ToolFailure("capture", error=detail), 80, Styles(None))
    assert "error 19" in "\n".join(rows)
    assert "\\x1b[2J" in rows[-1]
    assert "\x1b" not in "\n".join(rows)
    assert not any("more lines" in row for row in rows)


def test_source_bounds_survive_complete_selected_inline_text() -> None:
    result = ToolSuccess("capture", TextOutput("body\n" * 20, bounds=OutputBounds(truncated=True, source="artifact:42")))
    rows = render_tool_result(result, 80, Styles(None))
    assert rows[-1] == "[truncated, artifact:42]"
    assert rows[:-1] == ["body"] * 20 + [""]


def test_out_of_line_empty_output_keeps_its_human_summary() -> None:
    rows = render_tool_result(
        ToolSuccess(
            "capture",
            EmptyOutput(
                presentation="Captured both streams; originals are immutable.\x1b[2J",
                bounds=OutputBounds(reference=ArtifactReference("a" * 64, 65540, "text/plain")),
            ),
        ),
        100,
        Styles(None),
    )
    assert rows[0] == r"Captured both streams; originals are immutable.\x1b[2J"
    assert "\x1b" not in "\n".join(rows)
