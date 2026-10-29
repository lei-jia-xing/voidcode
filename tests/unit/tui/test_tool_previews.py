"""Compact tool previews preserve failures and expose display omissions."""

from voidcode.tui.styles import Styles
from voidcode.tui.views import render_tool_call, render_tool_result


def test_arguments_have_a_bounded_preview_with_history_access() -> None:
    rows = render_tool_call("write", {"path": "a.py", "content": "content " * 100}, 40, Styles(None))
    assert len(rows) == 4
    assert '"path": "a.py"' in "\n".join(rows)
    assert "arguments shortened" in rows[-1]
    assert '/show {"events":true}' in rows[-1]


def test_success_preview_omits_redundant_header_and_reports_hidden_lines() -> None:
    result = {"status": "ok", "output": {"type": "text", "text": "\n".join(f"line {i}" for i in range(20))}}
    rows = render_tool_result(result, 80, Styles(None))
    assert rows[:8] == [f"line {i}" for i in range(8)]
    assert "12 more lines" in rows[-1]
    assert not any("[ok]" in row for row in rows)


def test_errors_are_not_shortened_and_terminal_controls_are_safe() -> None:
    detail = "\n".join(f"error {i}" for i in range(20)) + "\x1b[2J"
    rows = render_tool_result({"status": "error", "error": detail}, 80, Styles(None))
    assert "error 19" in "\n".join(rows)
    assert "\\x1b[2J" in rows[-1]
    assert "\x1b" not in "\n".join(rows)
    assert not any("more lines" in row for row in rows)


def test_source_bounds_survive_success_preview() -> None:
    result = {"status": "ok", "output": {"type": "text", "text": "body\n" * 20, "bounds": {"truncated": True, "source": "artifact:42"}}}
    rows = render_tool_result(result, 80, Styles(None))
    assert rows[-1] == "[truncated, artifact:42]"
    assert any("more lines" in row for row in rows)
