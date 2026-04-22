from __future__ import annotations

from voidcode.runtime.context_window import (
    RuntimeContinuityState,
    _build_continuity_state,
)
from voidcode.tools.contracts import ToolResult


def _make_toolresult(kind: str, content: str | None = None) -> ToolResult:
    return ToolResult(
        tool_name="fake_tool",
        status=kind,
        content=content,
        data={},
        error=None,
    )


def test_continuity_state_includes_version_in_metadata_payload():
    # Create a dropped/retained scenario to exercise the continuity builder
    dropped = (_make_toolresult("ok"),)
    retained_count = 1
    state: RuntimeContinuityState = _build_continuity_state(
        dropped_results=dropped,
        retained_count=retained_count,
        preview_item_limit=2,
        preview_char_limit=80,
    )

    payload = state.metadata_payload()
    # Ensure the new version field is present and defaults to 1
    assert payload.get("version") == 1
    # Also ensure the other fields are present and sensible
    assert payload.get("source") == "tool_result_window"
    assert payload.get("dropped_tool_result_count") == 1
    assert payload.get("retained_tool_result_count") == retained_count
