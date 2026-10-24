"""Unit tests for the hooks plugin: config parsing, the gate, and the note surface.

Every hook command is a real subprocess (``sys.executable -c``), so the tests
exercise the ported executor through the plugin's own contract: a deny reason,
a bounded note, or ``None``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal

import pytest

from voidcode.plugins.hooks.hooks_plugin import MAX_NOTE_CHARACTERS, ToolHooks, parse_hooks_config
from voidcode.sdk.hooks import RunLifecycle, ToolIntent, ToolOutcome
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices

_EMIT_ARGV = "import json, sys; print(json.dumps(json.loads(sys.argv[1])))"


class _NoServices(PluginServices):
    """Hook leases are read-only: any state or event call is a bug for this plugin."""

    def get_state(self, scope: Literal["session", "workspace"] = "session"):  # type: ignore[no-untyped-def]
        raise AssertionError("hooks reads no state")

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        raise AssertionError("hooks reads no state")

    def replace_state(self, state, expected_version, scope: Literal["session", "workspace"] = "session"):  # type: ignore[no-untyped-def]
        raise AssertionError("hooks writes no state")

    def stage_event(self, event_type: str, payload) -> None:  # type: ignore[no-untyped-def]
        raise AssertionError("hooks stages no events")

    def read_events(self, *, after_sequence: int = 0, limit: int = 200):  # type: ignore[no-untyped-def]
        raise AssertionError("hooks reads no events")


def _action_command(payload: dict[str, object]) -> list[str]:
    return [sys.executable, "-c", _EMIT_ARGV, json.dumps(payload)]


def _marker_command(marker: Path, payload: dict[str, object] | None = None) -> list[str]:
    code = "import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran'); print(json.dumps(json.loads(sys.argv[2])))"
    return [sys.executable, "-c", code, str(marker), json.dumps({"action": "continue"} if payload is None else payload)]


def _surface_command(marker: Path, payload: dict[str, object] | None = None) -> list[str]:
    """A command that records the lifecycle environment the executor handed it."""
    code = (
        "import json, os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(os.environ['VOIDCODE_HOOK_SURFACE'] + '\\n' + os.environ['VOIDCODE_HOOK_PAYLOAD_JSON']); "
        "print(json.dumps(json.loads(sys.argv[2])))"
    )
    return [sys.executable, "-c", code, str(marker), json.dumps({"action": "continue"} if payload is None else payload)]


def _recorded_surface(marker: Path) -> tuple[str, dict[str, object]]:
    surface, payload_json = marker.read_text(encoding="utf-8").split("\n", 1)
    return surface, json.loads(payload_json)


def _lifecycle(phase: Literal["start", "end"]) -> RunLifecycle:
    return RunLifecycle(
        phase=phase,
        session_id="session-1",
        run_id="run-1",
        logical_turn="turn-1",
        status="completed" if phase == "end" else "",
    )


def _fail_command(message: str, *, code: int = 3) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.stderr.write({message!r}); sys.exit({code})"]


def _malformed_command() -> list[str]:
    return [sys.executable, "-c", "print('{not json')"]


def _slow_command(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _quiet_command() -> list[str]:
    return [sys.executable, "-c", "pass"]


def _intent(tool: str = "write") -> ToolIntent:
    return ToolIntent(tool_name=tool, tool_call_id="call-1", arguments={"path": "note.txt"})


def _outcome(tool: str = "write") -> ToolOutcome:
    return ToolOutcome(tool_name=tool, tool_call_id="call-1", status="ok", text="wrote note", arguments={"path": "note.txt"})


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


def test_config_parses_the_legacy_argv_shape_and_rejects_the_rest() -> None:
    default = parse_hooks_config(None)
    assert default.enabled is True
    assert default.timeout_seconds == 30.0
    assert default.pre_tool == () and default.post_tool == ()

    parsed = parse_hooks_config(
        {
            "timeout_seconds": 2.5,
            "pre_tool": [["gate", "--strict"], ["gate"]],
            "pre_tool_match": ["write*", "edit"],
            "post_tool": [["note"]],
            "post_tool_match": ["write"],
        }
    )
    assert parsed.timeout_seconds == 2.5
    assert parsed.pre_tool == (("gate", "--strict"), ("gate",))
    assert parsed.pre_tool_match == ("write*", "edit")
    assert parsed.post_tool == (("note",),)
    assert parsed.post_tool_match == ("write",)

    with pytest.raises(ValueError, match="field 'failure_mode' is not supported"):
        parse_hooks_config({"failure_mode": "fail"})
    with pytest.raises(ValueError, match="field 'enabled' must be a boolean"):
        parse_hooks_config({"enabled": "yes"})
    with pytest.raises(ValueError, match="field 'timeout_seconds' must be a positive number"):
        parse_hooks_config({"timeout_seconds": 0})
    with pytest.raises(ValueError, match="field 'pre_tool' must be an array of commands"):
        parse_hooks_config({"pre_tool": "gate --strict"})
    with pytest.raises(ValueError, match="field 'pre_tool' must contain non-empty string arrays"):
        parse_hooks_config({"pre_tool": [[]]})
    with pytest.raises(ValueError, match="field 'pre_tool' must contain non-empty string arrays"):
        parse_hooks_config({"pre_tool": [[""]]})
    with pytest.raises(ValueError, match="field 'post_tool_match' must be an array of strings"):
        parse_hooks_config({"post_tool_match": "write"})
    with pytest.raises(ValueError, match="field 'post_tool_match' must contain non-empty strings"):
        parse_hooks_config({"post_tool_match": [" "]})

    # The empty list is the documented "no commands" form, not an error.
    assert parse_hooks_config({"pre_tool": [], "post_tool_match": []}).pre_tool == ()


def test_config_parses_the_session_lifecycle_commands() -> None:
    default = parse_hooks_config(None)
    assert default.on_session_start == () and default.on_session_end == ()

    parsed = parse_hooks_config({"session_start": [["welcome"]], "session_end": [["farewell"], ["farewell", "--strict"]]})
    assert parsed.on_session_start == (("welcome",),)
    assert parsed.on_session_end == (("farewell",), ("farewell", "--strict"))

    with pytest.raises(ValueError, match="field 'session_end' must contain non-empty string arrays"):
        parse_hooks_config({"session_end": [[""]]})


def test_lifecycle_runs_the_configured_surface_with_the_legacy_environment(tmp_path: Path) -> None:
    start_marker = tmp_path / "start-surface.txt"
    end_marker = tmp_path / "end-surface.txt"
    hooks = ToolHooks(
        parse_hooks_config(
            {
                "session_start": [_surface_command(start_marker)],
                "session_end": [_surface_command(end_marker, {"guidance": "run ended"})],
            }
        )
    )
    assert hooks.lifecycle(_lifecycle("start"), _context(tmp_path), _NoServices()) is None
    assert hooks.lifecycle(_lifecycle("end"), _context(tmp_path), _NoServices()) == "run ended"

    start_surface, start_payload = _recorded_surface(start_marker)
    assert start_surface == "session_start"
    assert start_payload["phase"] == "start" and start_payload["run_id"] == "run-1"
    end_surface, end_payload = _recorded_surface(end_marker)
    assert end_surface == "session_end"
    assert end_payload["session_status"] == "completed"


def test_lifecycle_note_carries_diagnostics_and_reports_a_broken_command(tmp_path: Path) -> None:
    reported = ToolHooks(parse_hooks_config({"session_start": [_action_command({"diagnostic": "env checked", "guidance": "welcome"})]}))
    assert reported.lifecycle(_lifecycle("start"), _context(tmp_path), _NoServices()) == "welcome\nenv checked"

    failing = ToolHooks(parse_hooks_config({"session_end": [_fail_command("end hook exploded")]}))
    note = failing.lifecycle(_lifecycle("end"), _context(tmp_path), _NoServices())
    assert note is not None
    assert "lifecycle hook failed for session_end" in note


def test_lifecycle_surfaces_without_commands_run_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "never.txt"
    off = ToolHooks(parse_hooks_config({"enabled": False, "session_start": [_marker_command(marker)], "session_end": [_marker_command(marker)]}))
    assert off.lifecycle(_lifecycle("start"), _context(tmp_path), _NoServices()) is None
    assert off.lifecycle(_lifecycle("end"), _context(tmp_path), _NoServices()) is None
    assert not marker.exists()

    bare = ToolHooks(parse_hooks_config(None))
    assert bare.lifecycle(_lifecycle("start"), _context(tmp_path), _NoServices()) is None
    assert bare.lifecycle(_lifecycle("end"), _context(tmp_path), _NoServices()) is None
    assert not marker.exists()


def test_match_filter_selects_which_tools_a_surface_sees(tmp_path: Path) -> None:
    marker = tmp_path / "gate-ran.txt"
    hooks = ToolHooks(parse_hooks_config({"pre_tool": [_marker_command(marker)], "pre_tool_match": ["write*"]}))
    assert hooks.pre_tool(_intent("read_file"), _context(tmp_path), _NoServices()) is None
    assert not marker.exists(), "an unmatched tool never runs the gate command"
    assert hooks.pre_tool(_intent("write_file"), _context(tmp_path), _NoServices()) is None
    assert marker.read_text(encoding="utf-8") == "ran"

    note = tmp_path / "note-ran.txt"
    posting = ToolHooks(parse_hooks_config({"post_tool": [_marker_command(note)], "post_tool_match": ["write_file"]}))
    assert posting.post_tool(_outcome("read_file"), _context(tmp_path), _NoServices()) is None
    assert not note.exists()
    assert posting.post_tool(_outcome("write_file"), _context(tmp_path), _NoServices()) is None
    assert note.read_text(encoding="utf-8") == "ran"


def test_gate_denies_with_the_hook_diagnostic_and_allows_everything_else(tmp_path: Path) -> None:
    denying = ToolHooks(parse_hooks_config({"pre_tool": [_action_command({"action": "cancel", "diagnostic": "no writes today"})]}))
    reason = denying.pre_tool(_intent("write"), _context(tmp_path), _NoServices())
    assert reason is not None
    assert "no writes today" in reason
    assert "write" in reason

    allowing = ToolHooks(parse_hooks_config({"pre_tool": [_action_command({"action": "continue"})]}))
    assert allowing.pre_tool(_intent("write"), _context(tmp_path), _NoServices()) is None
    assert ToolHooks(parse_hooks_config({"pre_tool": [_quiet_command()]})).pre_tool(_intent("write"), _context(tmp_path), _NoServices()) is None

    # A cancel without a diagnostic still denies, with the legacy fallback text.
    bare = ToolHooks(parse_hooks_config({"pre_tool": [_action_command({"action": "cancel"})]}))
    bare_reason = bare.pre_tool(_intent("write"), _context(tmp_path), _NoServices())
    assert bare_reason is not None and "cancelled by pre-tool hook" in bare_reason


def test_a_broken_gate_denies_and_never_allows(tmp_path: Path) -> None:
    broken = ToolHooks(parse_hooks_config({"pre_tool": [_fail_command("gate exploded")]}))
    failure = broken.pre_tool(_intent("write"), _context(tmp_path), _NoServices())
    assert failure is not None
    assert "pre-hook failed" in failure and "non-zero exit status 3" in failure

    malformed = ToolHooks(parse_hooks_config({"pre_tool": [_malformed_command()]}))
    malformed_reason = malformed.pre_tool(_intent("write"), _context(tmp_path), _NoServices())
    assert malformed_reason is not None and "malformed JSON" in malformed_reason

    timed_out = ToolHooks(parse_hooks_config({"timeout_seconds": 0.05, "pre_tool": [_slow_command(1)]}))
    timeout_reason = timed_out.pre_tool(_intent("write"), _context(tmp_path), _NoServices())
    assert timeout_reason is not None and "timed out" in timeout_reason


def test_note_appends_guidance_and_stays_quiet_when_the_hook_fails(tmp_path: Path) -> None:
    guided = ToolHooks(parse_hooks_config({"post_tool": [_action_command({"action": "continue", "guidance": "file formatted"})]}))
    assert guided.post_tool(_outcome("write"), _context(tmp_path), _NoServices()) == "file formatted"

    quiet = ToolHooks(parse_hooks_config({"post_tool": [_quiet_command()]}))
    assert quiet.post_tool(_outcome("write"), _context(tmp_path), _NoServices()) is None

    # Fail open: a post-tool hook can never fail the write it observes.
    broken = ToolHooks(parse_hooks_config({"post_tool": [_fail_command("note exploded")]}))
    assert broken.post_tool(_outcome("write"), _context(tmp_path), _NoServices()) is None
    malformed = ToolHooks(parse_hooks_config({"post_tool": [_malformed_command()]}))
    assert malformed.post_tool(_outcome("write"), _context(tmp_path), _NoServices()) is None

    long_note = ToolHooks(parse_hooks_config({"post_tool": [_action_command({"guidance": "x" * (MAX_NOTE_CHARACTERS * 2)})]}))
    note = long_note.post_tool(_outcome("write"), _context(tmp_path), _NoServices())
    assert note is not None and len(note) == MAX_NOTE_CHARACTERS + len("[truncated]") and note.endswith("[truncated]")


def test_disabled_config_runs_no_hook_command(tmp_path: Path) -> None:
    ran = tmp_path / "ran.txt"
    hooks = ToolHooks(
        parse_hooks_config(
            {
                "enabled": False,
                "pre_tool": [_marker_command(ran, {"action": "cancel", "diagnostic": "blocked"})],
                "post_tool": [_marker_command(ran)],
            }
        )
    )
    assert hooks.pre_tool(_intent("write"), _context(tmp_path), _NoServices()) is None
    assert hooks.post_tool(_outcome("write"), _context(tmp_path), _NoServices()) is None
    assert not ran.exists()
