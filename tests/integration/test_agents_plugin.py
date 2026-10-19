"""End-to-end integration test for the Agents plugin on the real kernel stack.

Proves the plugin loads from its admitted artifact, finds its packaged prompt resources, and
serves the profile registry through the real command surface (no provider needed).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel

_AGENTS_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "agents"
_REVIEW_PROMPT = (_AGENTS_PLUGIN_ROOT / "profiles" / "review.txt").read_text(encoding="utf-8").strip()


_CREATED_KERNELS: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_created_kernels() -> Iterator[None]:
    yield
    while _CREATED_KERNELS:
        _CREATED_KERNELS.pop().close()


def _kernel(tmp_path: Path) -> Kernel:
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
            "agents": {"path": str(_AGENTS_PLUGIN_ROOT)},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=state_dir, trusted_plugins=frozenset({"agents"}))
    _CREATED_KERNELS.append(kernel)
    return kernel


def _invoke(kernel: Kernel, session_id: str, name: str, arguments: dict[str, object]):
    return kernel.invoke_command(session_id, name, arguments, expected_revision=kernel.command_catalog().revision_id)


def test_agents_command_lists_builtins_and_project_profile(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    session_id = kernel.create_session().session_id
    project_dir = tmp_path / "workspace" / ".voidcode" / "agents"
    project_dir.mkdir(parents=True)
    (project_dir / "helper.md").write_text(
        "---\nname: Helper\ndescription: Local helper\nmode: subagent\ntool_allowlist:\n  - read\n---\n\nHelp locally.\n",
        encoding="utf-8",
    )

    listed = _invoke(kernel, session_id, "agents", {})
    assert listed.status == "ok"
    assert listed.output is not None
    assert "[primary] default —" in listed.output
    assert "[subagent] review —" in listed.output
    assert "[subagent] helper — Local helper" in listed.output
    ids = [row["id"] for row in listed.data["profiles"]]  # type: ignore[index]
    assert {"default", "execute", "review", "explore", "research", "plan", "helper"} <= set(ids)


def test_agents_show_reads_packaged_prompt_from_the_admitted_artifact(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    session_id = kernel.create_session().session_id

    shown = _invoke(kernel, session_id, "agents.show", {"id": "review"})
    assert shown.status == "ok"
    assert shown.output == _REVIEW_PROMPT

    unknown = _invoke(kernel, session_id, "agents.show", {"id": "missing"})
    assert unknown.status == "error"
    assert unknown.error == "unknown agent profile: missing"
