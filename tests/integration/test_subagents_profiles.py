"""End-to-end integration test for the subagents plugin on the real core stack.

Proves the plugin loads from its admitted artifact and registers its tools and commands.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.core_factory import register_core
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core

_SUBAGENTS_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "subagents"
_REVIEW_PROMPT = (_SUBAGENTS_PLUGIN_ROOT / "profiles" / "texts" / "review.txt").read_text(encoding="utf-8").strip()


def _core(tmp_path: Path) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "policy": {"approval_mode": "ask"},
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            "subagents": {"path": str(_SUBAGENTS_PLUGIN_ROOT)},
        },
    }
    (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def test_subagents_plugin_registers_tools_and_commands(tmp_path: Path) -> None:
    core = _core(tmp_path)
    catalog = core.command_catalog()
    command_names = {cmd.name for cmd in catalog.commands}
    assert "subagents.monitor" in command_names
    assert "subagents.monitor.cancel" in command_names
    assert "agents" not in command_names
    assert "agents.show" not in command_names
    assert "delegation.monitor" not in command_names
