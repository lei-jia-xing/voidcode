"""End-to-end integration test for the hooks plugin's gate and note surfaces.

A real core admits the bundled ``hooks`` plugin next to an inline test plugin whose
``write`` tool really writes a file, so every assertion is about observable effects:
the file on disk, the events, and the text the model's next request carries.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

_HOOKS_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "hooks"
_NOTES_SOURCE = '''"""Test-only note writer."""

from __future__ import annotations

from pathlib import Path

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class WriteTool:
    definition = ToolDefinition(
        name="write",
        description="Write a small note file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        workspace = context.require_workspace()
        target = workspace / str(call.arguments["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(call.arguments["content"]), encoding="utf-8")
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text="wrote note"))


class ReadTool:
    definition = ToolDefinition(
        name="read_note",
        description="Read a small note file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        workspace = context.require_workspace()
        text = (workspace / str(call.arguments["path"])).read_text(encoding="utf-8")
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text=text))


def create_plugin(config):
    return Plugin(tools=(WriteTool(), ReadTool()))
'''
_EMIT_ARGV = "import json, sys; print(json.dumps(json.loads(sys.argv[1])))"
_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _action_command(payload: dict[str, object]) -> list[str]:
    return [sys.executable, "-c", _EMIT_ARGV, json.dumps(payload)]


def _notes_root(workspace: Path) -> Path:
    root = workspace / ".test-plugins" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(_NOTES_SOURCE, encoding="utf-8")
    return root


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    hooks_config: Mapping[str, object],
    *,
    read_only: bool = False,
) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "yolo", "read_only": read_only},
        "providers": {"endpoint": {"api_key": "loopback-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {
            "files": {"enabled": False},
            "notes": {"path": str(_notes_root(workspace))},
            "hooks": {"path": str(_HOOKS_ROOT), "config": dict(hooks_config)},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _write_replies() -> list[dict[str, object]]:
    return [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-note-1",
                        "type": "function",
                        "function": {"name": "write", "arguments": json.dumps({"path": "note.txt", "content": "hello"})},
                    }
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
        openai_response(
            message={"role": "assistant", "content": "note written"},
            finish="stop",
            usage={"prompt_tokens": 25, "completion_tokens": 5, "total_tokens": 30},
        ),
    ]


def _marker_command(marker: Path) -> list[str]:
    """A gate command whose only job is to prove it ran."""
    code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran'); print('{}')"
    return [sys.executable, "-c", code, str(marker)]


def _read_replies() -> list[dict[str, object]]:
    return [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-note-read",
                        "type": "function",
                        "function": {"name": "read_note", "arguments": json.dumps({"path": "note.txt"})},
                    }
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
        openai_response(
            message={"role": "assistant", "content": "note read"},
            finish="stop",
            usage={"prompt_tokens": 25, "completion_tokens": 5, "total_tokens": 30},
        ),
    ]


def _model_messages(server: OpenAILoopbackServer) -> list[str]:
    """Every message text the model was actually sent, oldest first."""
    texts: list[str] = []
    for request in server.requests:
        for message in request.get("messages", ()):  # type: ignore[union-attr]
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                texts.append(str(message["content"]))
    return texts


def test_pre_tool_hook_denies_the_write_before_it_runs(tmp_path: Path) -> None:
    diagnostic = "hooks-rule: this workspace is frozen"
    with _loopback_server(_write_replies()) as server:
        core = _core(tmp_path, server, {"pre_tool": [_action_command({"action": "cancel", "diagnostic": diagnostic})]})
        session_id = core.create_session().session_id
        assert core.run("write a note", session_id=session_id).status == "completed"

        # The hook's diagnostic is what the model reads instead of a result.
        assert any(diagnostic in text for text in _model_messages(server)), _model_messages(server)

        # Denied before effects: no intent was recorded, the call never completed ok, and no file exists.
        events = list(core.events_after(session_id))
        assert not [event for event in events if event.event_type == "core.tool_intent"]
        results = [dict(event.payload)["result"] for event in events if event.event_type == "core.tool_completed"]
        assert [result["status"] for result in results] == ["error"]  # type: ignore[index]
        assert diagnostic in str(results[0]["error"])  # type: ignore[index]
        assert not (tmp_path / "workspace" / "note.txt").exists()


def test_post_tool_hook_appends_its_guidance_to_the_successful_result(tmp_path: Path) -> None:
    guidance = "hooks-note: run the formatter next"
    with _loopback_server(_write_replies()) as server:
        core = _core(
            tmp_path,
            server,
            {"post_tool": [_action_command({"action": "continue", "guidance": guidance})], "post_tool_match": ["write"]},
        )
        session_id = core.create_session().session_id
        assert core.run("write a note", session_id=session_id).status == "completed"

        assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "hello"
        texts = [text for text in _model_messages(server) if "wrote note" in text]
        assert texts, _model_messages(server)
        assert texts[0].endswith(guidance), texts[0]


def test_disabled_hooks_config_leaves_both_paths_normal(tmp_path: Path) -> None:
    diagnostic = "hooks-rule: this workspace is frozen"
    guidance = "hooks-note: run the formatter next"
    with _loopback_server(_write_replies()) as server:
        core = _core(
            tmp_path,
            server,
            {
                "enabled": False,
                "pre_tool": [_action_command({"action": "cancel", "diagnostic": diagnostic})],
                "post_tool": [_action_command({"action": "continue", "guidance": guidance})],
            },
        )
        session_id = core.create_session().session_id
        assert core.run("write a note", session_id=session_id).status == "completed"

        assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "hello"
        texts = _model_messages(server)
        assert any("wrote note" in text for text in texts)
        assert not any(text.endswith(guidance) for text in texts)
        assert not any(diagnostic in text for text in texts)


def test_no_commands_configured_is_a_transparent_no_op(tmp_path: Path) -> None:
    with _loopback_server(_write_replies()) as server:
        core = _core(tmp_path, server, {"timeout_seconds": 5})
        session_id = core.create_session().session_id
        assert core.run("write a note", session_id=session_id).status == "completed"
        assert (tmp_path / "workspace" / "note.txt").read_text(encoding="utf-8") == "hello"
        texts = [text for text in _model_messages(server) if "wrote note" in text]
        assert texts == ["wrote note"]


def test_session_lifecycle_hooks_run_for_their_side_effects(tmp_path: Path) -> None:
    start_marker = tmp_path / "start-ran.txt"
    end_marker = tmp_path / "end-ran.txt"
    with _loopback_server(_write_replies()) as server:
        core = _core(
            tmp_path,
            server,
            {
                "session_start": [_marker_command(start_marker)],
                "session_end": [_marker_command(end_marker), _action_command({"guidance": "session finished"})],
            },
        )
        session_id = core.create_session().session_id
        assert core.run("write a note", session_id=session_id).status == "completed"

        # Both surfaces ran their argv commands for real.
        assert start_marker.read_text(encoding="utf-8") == "ran"
        assert end_marker.read_text(encoding="utf-8") == "ran"

        # Each fired exactly once and left its note as the durable record (never prompt context).
        invoked = [dict(event.payload) for event in core.events_after(session_id) if event.event_type == "core.hook_invoked"]
        assert [payload["hook_kind"] for payload in invoked] == ["session_start", "session_end"]
        assert {payload["owner"] for payload in invoked} == {"hooks"}
        assert invoked[1]["note"] == "session finished"


def test_read_only_policy_skips_a_matching_gate_and_notes_it_on_the_result(tmp_path: Path) -> None:
    marker = tmp_path / "gate-ran.txt"
    with _loopback_server(_read_replies()) as server:
        core = _core(tmp_path, server, {"post_tool": [_marker_command(marker)]}, read_only=True)
        (tmp_path / "workspace" / "note.txt").write_text("note contents", encoding="utf-8")
        session_id = core.create_session().session_id
        assert core.run("read the note", session_id=session_id).status == "completed"

        # The read-tier call ran normally, and its result carries the core's skip clause.
        texts = [text for text in _model_messages(server) if "note contents" in text]
        assert texts, _model_messages(server)
        assert texts[-1].endswith("[hook hooks skipped: read-only policy]"), texts[-1]

        # The configured hook command never ran: a mutating argv hook is skipped under read-only.
        assert not marker.exists()
        assert [event.event_type for event in core.events_after(session_id)].count("core.tool_completed") == 1


def test_read_only_read_ignores_a_write_only_hook(tmp_path: Path) -> None:
    marker = tmp_path / "gate-ran.txt"
    with _loopback_server(_read_replies()) as server:
        core = _core(
            tmp_path,
            server,
            {"post_tool": [_marker_command(marker)], "post_tool_match": ["write"]},
            read_only=True,
        )
        (tmp_path / "workspace" / "note.txt").write_text("note contents", encoding="utf-8")
        session_id = core.create_session().session_id
        assert core.run("read the note", session_id=session_id).status == "completed"

        # A write-only hook never touches a read: no note, no run, exact read text.
        texts = [text for text in _model_messages(server) if "note contents" in text]
        assert texts == ["note contents"], _model_messages(server)
        assert not marker.exists()


def test_read_only_read_with_no_hooks_configured_is_exact(tmp_path: Path) -> None:
    with _loopback_server(_read_replies()) as server:
        core = _core(tmp_path, server, {"timeout_seconds": 5}, read_only=True)
        (tmp_path / "workspace" / "note.txt").write_text("note contents", encoding="utf-8")
        session_id = core.create_session().session_id
        assert core.run("read the note", session_id=session_id).status == "completed"

        # Empty hooks config registers nothing: the read result is byte-exact, no skip-note.
        texts = [text for text in _model_messages(server) if "note contents" in text]
        assert texts == ["note contents"], _model_messages(server)
