"""End-to-end integration test for the delegation-owned profile commands on the real core stack.

Proves the plugin loads from its admitted artifact, finds its packaged prompt resources, and
serves the profile registry through the real command surface (no provider needed).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

_DELEGATION_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "delegation"
_REVIEW_PROMPT = (_DELEGATION_PLUGIN_ROOT / "profiles" / "texts" / "review.txt").read_text(encoding="utf-8").strip()


_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


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
            "delegation": {"path": str(_DELEGATION_PLUGIN_ROOT)},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def test_agents_command_lists_builtins_and_project_profile(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session_id = core.create_session().session_id
    project_dir = tmp_path / "workspace" / ".voidcode" / "agents"
    project_dir.mkdir(parents=True)
    (project_dir / "helper.md").write_text(
        "---\nname: Helper\ndescription: Local helper\nmode: subagent\ntool_allowlist:\n  - read\n---\n\nHelp locally.\n",
        encoding="utf-8",
    )

    listed = _invoke(core, session_id, "agents", {})
    assert listed.status == "ok"
    assert listed.output is not None
    assert "[primary] default —" in listed.output
    assert "[subagent] review —" in listed.output
    assert "[subagent] helper — Local helper" in listed.output
    ids = [row["id"] for row in listed.data["profiles"]]  # type: ignore[index]
    assert {"default", "execute", "review", "explore", "research", "plan", "helper"} <= set(ids)


def test_agents_show_reads_packaged_prompt_from_the_admitted_artifact(tmp_path: Path) -> None:
    core = _core(tmp_path)
    session_id = core.create_session().session_id

    shown = _invoke(core, session_id, "agents.show", {"id": "review"})
    assert shown.status == "ok"
    assert shown.output == _REVIEW_PROMPT

    unknown = _invoke(core, session_id, "agents.show", {"id": "missing"})
    assert unknown.status == "error"
    assert unknown.error == "unknown agent profile: missing"
