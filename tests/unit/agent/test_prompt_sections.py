from __future__ import annotations

from voidcode.agent.prompt_sections import (
    tool_inventory_clarity_line,
    user_append_heading_block,
)


def test_user_append_heading_block_marks_user_text_authoritative() -> None:
    block = user_append_heading_block()

    assert block
    assert "authoritative" in block.lower()


def test_tool_inventory_clarity_line_marks_inventory_advisory() -> None:
    line = tool_inventory_clarity_line()

    assert line
    assert "advisory" in line.lower()
