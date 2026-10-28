"""CLI boundaries: root values are never commands, client-only flags stay bare.

The read-only `show` surface exercises the real `main` argv path against an owned
SQLite fixture: no core, no config, no runtime, no provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voidcode.cli import main
from voidcode.core.storage.store import SqliteStore


def _show_fixture(workspace: Path, state_dir: Path, session_id: str) -> str:
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    with SqliteStore(state_dir / "voidcode.db") as store:
        revision_id = store.put_revision({"marker": "cli-boundary"})
        store.record_workspace_revision(str(workspace.resolve()), revision_id)
        store.create_session(session_id, str(workspace.resolve()), revision_id)
    return revision_id


def test_show_accepts_root_options_after_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    revision_id = _show_fixture(workspace, state, "boundary-session")
    assert main(["show", "boundary-session", "--workspace", str(workspace.resolve()), "--state-dir", str(state)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "boundary-session" and payload["revision_id"] == revision_id


def test_root_value_named_like_a_command_is_never_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    revision_id = _show_fixture(tmp_path / "run", tmp_path / "state", "boundary-session")
    assert main(["--workspace", "run", "show", "boundary-session", "--state-dir", str(tmp_path / "state")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "boundary-session" and payload["revision_id"] == revision_id


def test_bare_client_flag_before_a_command_is_rejected(tmp_path: Path) -> None:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    _show_fixture(workspace, state, "other")
    with pytest.raises(SystemExit) as refused:
        main(["--session-id", "bare-client-session", "show", "other", "--workspace", str(workspace.resolve()), "--state-dir", str(state)])
    assert refused.value.code == 2


def test_removed_tui_command_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SystemExit) as refused:
        main(["tui", "--workspace", str(workspace.resolve())])
    assert refused.value.code == 2


def test_state_directory_structure(tmp_path: Path) -> None:
    from voidcode.bootstrap import state_directory

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = state_directory(workspace)
    assert state_dir.parent.name == "voidcode"


@pytest.mark.parametrize("raw", ["-1", "nan", "inf"])
def test_bad_wait_timeout_rejected_before_any_state_exists(tmp_path: Path, raw: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    assert (
        main(
            [
                "invoke",
                "s1",
                "workflow.run",
                "--arguments",
                '{"workflow": "x"}',
                "--expected-revision",
                "rev",
                "--wait-background",
                "--wait-timeout",
                raw,
                "--workspace",
                str(workspace.resolve()),
                "--state-dir",
                str(state_dir),
            ]
        )
        == 1
    )
    assert not state_dir.exists(), "a rejected timeout must not create state or run anything"
