"""Unit tests for the formatter plugin's config merge and post-tool hook."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Literal

import pytest

from voidcode.plugins.formatter.formatter_plugin import (
    FORMATTED_TOOL_NAMES,
    FormatterHook,
    create_plugin,
    parse_formatter_config,
)
from voidcode.sdk.hooks import ToolOutcome
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

_MANIFEST = Path(__file__).resolve().parents[3] / "src" / "voidcode" / "plugins" / "formatter" / "voidcode-plugin.json"


class _NoServices(PluginServices):
    def get_state(self, scope: Literal["session", "workspace"] = "session"):
        raise AssertionError("formatter reads no state")

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        raise AssertionError("formatter reads no state")

    def replace_state(self, state, expected_version, scope: Literal["session", "workspace"] = "session"):  # type: ignore[no-untyped-def]
        raise AssertionError("formatter writes no state")

    def stage_event(self, event_type: str, payload) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("formatter stages no events")


def _outcome(path: str | None, *, tool: str = "write", status: str = "ok") -> ToolOutcome:
    arguments = {} if path is None else {"path": path}
    return ToolOutcome(
        tool_name=tool,
        tool_call_id="call-1",
        status=status,  # type: ignore[arg-type]
        text="wrote file successfully",
        arguments=arguments,
    )


def _context(workspace: Path) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-1",
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="model",
        logical_turn="turn-1",
    )


def _append_command(marker: str) -> list[str]:
    return [sys.executable, "-c", f"import sys; open(sys.argv[1], 'a').write({marker!r})"]


def _fail_command(message: str) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.stderr.write({message!r}); sys.exit(3)"]


def test_config_defaults_disable_formatting_and_merge_languages() -> None:
    base = parse_formatter_config(None)
    assert base.format_on_write is False
    assert base.resolve_formatter(Path("x.py")) is not None, "builtin presets stay available"

    merged = parse_formatter_config(
        {
            "format_on_write": True,
            "languages": {".txt": {"command": ["fmt-txt"], "cwd": "workspace"}},
        }
    )
    assert merged.format_on_write is True
    resolved = merged.resolve_formatter(Path("note.txt"))
    assert resolved is not None
    assert resolved[0] == ".txt"
    assert resolved[1].command == ("fmt-txt",)

    with pytest.raises(ValueError, match="field 'other' is not supported"):
        parse_formatter_config({"other": 1})
    with pytest.raises(ValueError, match="suffixes like '.py'"):
        parse_formatter_config({"languages": {"py": {"command": ["x"]}}})
    with pytest.raises(ValueError, match="field 'cwd' must be one of"):
        parse_formatter_config({"languages": {".txt": {"command": ["x"], "cwd": "somewhere"}}})


def test_hook_requires_opt_in_and_a_writable_path(tmp_path: Path) -> None:
    disabled = FormatterHook(parse_formatter_config({"languages": {".txt": {"command": _append_command("x")}}}))
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    assert disabled(_outcome("note.txt"), _context(tmp_path), _NoServices()) is None
    assert target.read_text(encoding="utf-8") == "hello"

    enabled = FormatterHook(parse_formatter_config({"format_on_write": True, "languages": {".txt": {"command": _append_command("[formatted]")}}}))
    assert enabled(_outcome("note.txt"), _context(tmp_path), _NoServices()) is None, "success is visible in the file"
    assert target.read_text(encoding="utf-8") == "hello[formatted]"

    # No path argument, an unmapped suffix, a failed tool result and an escaping path are all no-ops.
    assert enabled(_outcome(None), _context(tmp_path), _NoServices()) is None
    assert enabled(_outcome("note.unknown"), _context(tmp_path), _NoServices()) is None
    assert enabled(_outcome("note.txt", status="error"), _context(tmp_path), _NoServices()) is None
    assert enabled(_outcome("../outside.txt"), _context(tmp_path), _NoServices()) is None


def test_hook_reports_a_warning_instead_of_failing_the_write(tmp_path: Path) -> None:
    hook = FormatterHook(
        parse_formatter_config({"format_on_write": True, "languages": {".txt": {"command": _fail_command("boom"), "cwd": "workspace"}}})
    )
    target = tmp_path / "note.txt"
    target.write_text("hello", encoding="utf-8")
    note = hook(_outcome("note.txt"), _context(tmp_path), _NoServices())
    assert note is not None
    assert note.startswith("Formatter warning: ")
    assert "boom" in note


def test_manifest_declares_the_mutating_post_tool_hook_for_the_write_tools() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    hooks = manifest["hooks"]
    assert len(hooks) == 1
    assert hooks[0]["kind"] == "post_tool"
    assert hooks[0]["mutating"] is True, "the kernel must be able to skip it under a restrictive stance"
    assert tuple(hooks[0]["match"]) == FORMATTED_TOOL_NAMES
    plugin = create_plugin({"format_on_write": True})
    assert plugin.post_tool is not None
    assert plugin.tools == ()
    assert plugin.commands == ()


def test_hook_uses_the_first_available_path_argument_key(tmp_path: Path) -> None:
    hook = FormatterHook(parse_formatter_config({"format_on_write": True, "languages": {".txt": {"command": _append_command("[f]")}}}))
    target = tmp_path / "note.txt"
    target.write_text("x", encoding="utf-8")
    outcome = ToolOutcome(
        tool_name="edit",
        tool_call_id="call-1",
        status="ok",
        text="edited",
        arguments={"file_path": "note.txt"},
    )
    assert hook(outcome, _context(tmp_path), _NoServices()) is None
    assert target.read_text(encoding="utf-8") == "x[f]"
    shutil.rmtree(tmp_path / "unused", ignore_errors=True)
