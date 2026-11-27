"""End-to-end integration tests for the Plan plugin's restrictive overlay.

Proves on the real core stack that entering plan mode hides and denies write-tier tools,
contributes the verbatim plan instruction, survives a restart, and that leaving plan mode
restores writes while a user-frozen read-only policy stays in force.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import tomllib
from pathlib import Path

from tests.fixtures.core_factory import discard_core, loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.core.storage.store import SequenceConflict
from voidcode.plugins.plan.plan_plugin import PLAN_MODE_INSTRUCTION

_PLAN_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "plan"
_WRITE_TOOL_PLUGIN_SOURCE = '''"""Test-only plugin exposing one write-tier tool."""

from __future__ import annotations

from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess


class NotesWriteTool:
    definition = ToolDefinition(
        name="notes_write",
        description="Write a note (test write-tier tool).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def invoke(self, call, *, context, services):
        return ToolSuccess(tool_name=self.definition.name, output=TextOutput(text="notes written"))


def create_plugin(config):
    return Plugin(tools=(NotesWriteTool(),))
'''


def _write_tool_plugin(workspace: Path) -> Path:
    root = workspace / ".test-plugins" / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(_WRITE_TOOL_PLUGIN_SOURCE, encoding="utf-8")
    return root


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    read_only: bool = False,
    state_dir: Path | None = None,
    plan_enabled: bool = True,
) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    resolved_state = state_dir if state_dir is not None else tmp_path / "state"
    resolved_state.mkdir(parents=True, exist_ok=True)
    notes_root = _write_tool_plugin(workspace)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "ask", "read_only": read_only},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            "files": {"enabled": False},
            "plan": {"path": str(_PLAN_PLUGIN_ROOT), "enabled": plan_enabled},
            "notes": {"path": str(notes_root)},
        },
    }
    (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")
    core = register_core(open_core(workspace, state_dir=resolved_state))
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def _tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for entry in tools:
        if isinstance(entry, dict) and isinstance(entry.get("function"), dict):
            names.append(str(entry["function"]["name"]))  # type: ignore[index]
    return names


def _text(request: dict[str, object]) -> str:
    return json.dumps(request.get("messages"), ensure_ascii=False)


def _seed_legacy_plan_state(state_dir: Path, session_id: str, revision_id: str) -> None:
    with sqlite3.connect(state_dir / "voidcode.db") as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO plugin_state
                (scope, scope_key, owner, version, schema_version, revision_id, payload_json, updated_at)
            VALUES ('session', ?, 'plan', 1, 1, ?, ?, 0)
            """,
            (session_id, revision_id, json.dumps({"schema_version": 99, "active": "not-a-boolean"})),
        )


def test_plan_mode_hides_and_denies_write_tools_until_plan_off(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(
            message={"role": "assistant", "content": "planned"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
        openai_response(
            message={"role": "assistant", "content": "wrote nothing"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
        openai_response(
            message={"role": "assistant", "content": "plain turn"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id

        # Baseline: the write-tier tool is advertised and the plan instruction is absent.
        assert core.run("hi", session_id=session_id).status == "completed"
        assert "notes_write" in _tool_names(server.requests[0])
        assert PLAN_MODE_INSTRUCTION not in _text(server.requests[0])

        # Enter plan mode: the overlay hides the write tool and contributes the instruction.
        assert core.overlay_active(session_id, "plan") is False
        entered = _invoke(core, session_id, "plan", {})
        assert entered.status == "ok"
        assert entered.data == {"active": True, "changed": True}
        assert core.overlay_active(session_id, "plan") is True
        assert core.session_read_only(session_id) is True
        assert _invoke(core, session_id, "plan.status", {}).output == "Plan mode: active"

        assert core.run("plan it", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[1])
        assert PLAN_MODE_INSTRUCTION in _text(server.requests[1])

        # The overlay survives a restart: still hidden, still instructed.
        core.close()
        discard_core(core)
        reopened = _core(tmp_path, server, state_dir=tmp_path / "state")
        assert _invoke(reopened, session_id, "plan.status", {}).output == "Plan mode: active"
        assert reopened.run("still planning", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[2])
        assert PLAN_MODE_INSTRUCTION in _text(server.requests[2])

        # Leaving plan mode revokes only this owner's overlay.
        left = _invoke(reopened, session_id, "plan.off", {})
        assert left.status == "ok"
        assert left.data == {"active": False, "changed": True}
        assert reopened.overlay_active(session_id, "plan") is False
        assert reopened.session_read_only(session_id) is False
        assert _invoke(reopened, session_id, "plan.status", {}).output == "Plan mode: inactive"

        replies.append(
            openai_response(
                message={"role": "assistant", "content": "back to normal"},
                finish="stop",
                usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
            )
        )
        assert reopened.run("normal again", session_id=session_id).status == "completed"
        assert "notes_write" in _tool_names(server.requests[3])
        assert PLAN_MODE_INSTRUCTION not in _text(server.requests[3])

        offline = _invoke(reopened, session_id, "plan.off", {})
        assert offline.data == {"active": False, "changed": False}


def test_plan_off_never_lifts_a_user_frozen_read_only(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "read-only turn"},
            finish="stop",
            usage={"prompt_tokens": 15, "completion_tokens": 4, "total_tokens": 19},
        )
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, read_only=True)
        session_id = core.create_session().session_id
        assert _invoke(core, session_id, "plan", {}).status == "ok"
        off = _invoke(core, session_id, "plan.off", {})
        assert off.status == "ok"
        assert "still read-only for another reason" in off.output
        status = _invoke(core, session_id, "plan.status", {})
        assert status.output == "Plan mode: inactive\nThe session is still read-only for another reason (frozen policy or another owner's overlay)."
        assert status.data == {"active": False, "read_only": True}
        assert core.overlay_active(session_id, "plan") is False
        assert core.session_read_only(session_id) is True
        assert core.run("still frozen", session_id=session_id).status == "completed"
        assert "notes_write" not in _tool_names(server.requests[0]), "the frozen policy outlives the plan overlay"


def test_plan_ignores_absent_and_malformed_legacy_state(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id

        # Absent state is the normal first-run case: plan mode is simply inactive.
        status = _invoke(core, session_id, "plan.status", {})
        assert status.output == "Plan mode: inactive"
        assert status.data == {"active": False, "read_only": False}

        # A row from the old stateful plan plugin is deliberately unreadable by the new
        # implementation. It must not block status, entry, context, or revocation.
        _seed_legacy_plan_state(tmp_path / "state", session_id, core.revision_id)
        assert _invoke(core, session_id, "plan.status", {}).output == "Plan mode: inactive"
        assert _invoke(core, session_id, "plan", {}).data["changed"] is True
        assert core.overlay_active(session_id, "plan") is True
        contributions = core.plugin_runtime.contribute_context(session_id)
        assert any(PLAN_MODE_INSTRUCTION in str(item.get("content", "")) for item in contributions)
        assert _invoke(core, session_id, "plan.off", {}).data["changed"] is True
        assert core.overlay_active(session_id, "plan") is False

        core.close()
        discard_core(core)
        reopened = _core(tmp_path, server, state_dir=tmp_path / "state")
        assert _invoke(reopened, session_id, "plan.status", {}).output == "Plan mode: inactive"
        assert _invoke(reopened, session_id, "plan", {}).status == "ok"
        assert reopened.overlay_active(session_id, "plan") is True

        # The legacy row also must not block dropping the owner and deleting its overlay.
        config_path = tmp_path / "workspace" / "voidcode.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        config["plugins"]["plan"]["enabled"] = False
        config_path.write_text(dumps_toml(config), encoding="utf-8")
        switched = _invoke(reopened, session_id, "core.reload", {})
        assert switched.status == "ok", switched.error
        assert reopened.overlay_active(session_id, "plan") is False
        assert reopened.session_read_only(session_id) is False


def test_concurrent_plan_enter_and_off_keep_overlay_status_and_enforcement_in_sync(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id
        barrier = threading.Barrier(2)
        failures: list[str] = []

        def worker(name: str) -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(20):
                    try:
                        result = _invoke(core, session_id, name, {})
                    except SequenceConflict:
                        # Concurrent client commands can lose the generic event-sequence CAS;
                        # the overlay mutation itself is still last-writer and is the truth.
                        continue
                    if result.status != "ok" and "SequenceConflict" not in str(result.error):
                        failures.append(f"{name}: {result.error}")
            except Exception as exc:  # pragma: no cover - surfaced through failures
                failures.append(f"{name}: {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("plan", "plan.off")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not any(thread.is_alive() for thread in threads)
        assert failures == []

        active = core.overlay_active(session_id, "plan")
        status = _invoke(core, session_id, "plan.status", {})
        assert status.data == {"active": active, "read_only": active}
        assert core.session_read_only(session_id) is active
        assert (core.store.session_overlays(session_id) == ("read-only",)) is active
        contributions = core.plugin_runtime.contribute_context(session_id)
        assert (any(PLAN_MODE_INSTRUCTION in str(item.get("content", "")) for item in contributions)) is active


def test_disabling_plan_revokes_only_its_overlay(tmp_path: Path) -> None:
    with loopback_server([]) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id
        assert _invoke(core, session_id, "plan", {}).status == "ok"
        assert core.overlay_active(session_id, "plan") is True
        core.store.set_session_overlay(session_id, "other-owner")

        config_path = tmp_path / "workspace" / "voidcode.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        config["plugins"]["plan"]["enabled"] = False
        config_path.write_text(dumps_toml(config), encoding="utf-8")

        switched = _invoke(core, session_id, "core.reload", {})
        assert switched.status == "ok", switched.error
        assert core.overlay_active(session_id, "plan") is False
        assert core.overlay_active(session_id, "other-owner") is True
        assert core.session_read_only(session_id) is True
        assert "plan" not in {command.name.split(".")[0] for command in core.command_catalog().commands}
        core.store.clear_session_overlay(session_id, "other-owner")
        assert core.session_read_only(session_id) is False
