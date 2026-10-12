from __future__ import annotations

from voidcode.agent.prompt_sections import user_append_heading_block


def test_user_append_heading_block_marks_user_text_authoritative() -> None:
    block = user_append_heading_block()

    assert block
    assert "authoritative" in block.lower()


def test_dynamic_boundary_marker_is_a_stable_prompt_anchor() -> None:
    from voidcode.agent.prompt_sections import dynamic_boundary_marker

    assert dynamic_boundary_marker() == "<!-- voidcode:dynamic-boundary -->"
