"""CLI boundaries: root values are never commands, client-only flags stay bare.

The read-only `show` surface exercises the real `main` argv path against an owned
SQLite fixture: no core, no config, no runtime, no provider.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voidcode.bootstrap import state_directory
from voidcode.cli import _resolve_command_name, main
from voidcode.core.storage.store import SqliteStore
from voidcode.sdk.commands import CommandCatalog, CommandDescriptor


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


def test_show_accepts_inline_root_options(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    revision_id = _show_fixture(workspace, state, "boundary-session")
    assert main(["show", "boundary-session", f"--workspace={workspace.resolve()}", f"--state-dir={state}"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "boundary-session" and payload["revision_id"] == revision_id


def test_root_value_after_the_command_named_like_a_command_is_never_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    revision_id = _show_fixture(tmp_path / "serve", tmp_path / "state", "boundary-session")
    assert main(["show", "boundary-session", "--state-dir", str(tmp_path / "state"), "--workspace", "serve"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == "boundary-session" and payload["revision_id"] == revision_id


def test_double_dash_prompt_is_not_read_as_a_root_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode import cli

    seen: dict[str, object] = {}

    class _FakeCore:
        def __enter__(self) -> _FakeCore:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def fake_open_core(workspace: Path, *, state_dir: Path | None = None) -> _FakeCore:
        seen["workspace"] = workspace
        seen["state_dir"] = state_dir
        return _FakeCore()

    def fake_dispatch(core: object, args: argparse.Namespace) -> int:
        seen["prompt"] = args.prompt
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "open_core", fake_open_core)
    monkeypatch.setattr(cli, "_dispatch", fake_dispatch)
    assert main(["run", "--", "--workspace"]) == 0
    assert seen["prompt"] == "--workspace"
    assert seen["workspace"] == tmp_path.resolve()
    assert seen["state_dir"] == state_directory(tmp_path.resolve())


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


def test_tui_prefs_read_the_workspace_file(tmp_path: Path) -> None:
    from argparse import Namespace

    from voidcode.cli import _tui_prefs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(
        json.dumps({"schema_version": 1, "tui": {"border": "box", "footer": "compact", "show_session": True}}),
        encoding="utf-8",
    )
    prefs = _tui_prefs(Namespace(server=None, no_border=False, footer=None, show_session=False), workspace=workspace)
    assert (prefs["border"], prefs["footer"], prefs["show_session"]) == ("box", "compact", True)


def test_tui_prefs_fail_visibly_on_a_malformed_file(tmp_path: Path) -> None:
    from argparse import Namespace

    from voidcode.cli import _tui_prefs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read TUI preferences"):
        _tui_prefs(Namespace(server=None, no_border=False, footer=None, show_session=False), workspace=workspace)


def _catalog(*names: str) -> SimpleNamespace:
    commands = tuple(CommandDescriptor(name=name, owner="p", description=name) for name in names)
    catalog = CommandCatalog(revision_id="rev-1", commands=commands)
    return SimpleNamespace(command_catalog=lambda: catalog)


def test_invoke_resolves_public_path_to_the_canonical_id() -> None:
    core = _catalog("goal", "goal.set", "core.reload")
    assert _resolve_command_name(core, "goal.set") == "goal.set"
    assert _resolve_command_name(core, "goal  set") == "goal.set"
    assert _resolve_command_name(core, "core reload") == "core.reload"
    assert _resolve_command_name(core, "missing command") == "missing command"


def test_invoke_refuses_an_ambiguous_public_path() -> None:
    core = _catalog("goal.set", "goal set")
    with pytest.raises(ValueError, match="Ambiguous command path: goal set"):
        _resolve_command_name(core, "goal set")


def test_bare_client_startup_error_writes_stderr_without_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "does-not-exist"
    state = tmp_path / "state"
    assert main(["--workspace", str(missing), "--state-dir", str(state)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Error:")
