"""Forwarding contracts for the ``serve``, ``web`` and ``tui`` CLI commands.

These run ``app.main(argv)`` in-process: the runtime-config seam is stubbed so
the command does not read a real config, and the server/TUI entry callables are
replaced so nothing binds a socket or starts a UI.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from voidcode.cli_support import EXIT_SUCCESS

from ._cli_harness import RUNTIME_SEAM, deterministic_config

# ``app`` owns the entrypoint; the server command module owns the ``serve``/``web``
# entry callables the commands forward to.
CLI_MODULE: Any = importlib.import_module("voidcode.cli.app")
SERVER_COMMAND_MODULE: Any = importlib.import_module("voidcode.cli.commands.server")


@contextmanager
def recorded_config_loader() -> Iterator[dict[str, Any]]:
    """Patch the config load to echo the requested approval mode.

    Records what the command asked for and returns a deterministic config shaped
    the same way the real loader shapes it for that approval mode.
    """
    seen: dict[str, Any] = {}

    def load(workspace: Path, **kwargs: Any) -> Any:
        approval_mode = kwargs.get("approval_mode")
        config = replace(deterministic_config(), approval_mode=approval_mode or "deny")
        seen["workspace"] = workspace
        seen["approval_mode"] = approval_mode
        seen["config"] = config
        return config

    with patch.object(RUNTIME_SEAM, "load_runtime_config", side_effect=load):
        yield seen


@pytest.mark.parametrize("approval_mode", ["allow", "deny", "ask"])
def test_serve_forwards_workspace_host_port_and_approval_mode(approval_mode: str, tmp_path: Path) -> None:
    with recorded_config_loader() as seen:
        with patch.object(SERVER_COMMAND_MODULE, "serve", autospec=True) as serve_entry:
            result = CLI_MODULE.main(
                [
                    "serve",
                    "--workspace",
                    str(tmp_path),
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "9000",
                    "--approval-mode",
                    approval_mode,
                ]
            )

    assert result == EXIT_SUCCESS
    assert seen["workspace"] == tmp_path
    assert seen["approval_mode"] == approval_mode
    assert serve_entry.call_args.kwargs == {
        "workspace": tmp_path,
        "host": "0.0.0.0",
        "port": 9000,
        "config": seen["config"],
    }
    assert seen["config"].approval_mode == approval_mode


def test_serve_without_approval_mode_requests_no_override(tmp_path: Path) -> None:
    with recorded_config_loader() as seen:
        with patch.object(SERVER_COMMAND_MODULE, "serve", autospec=True) as serve_entry:
            result = CLI_MODULE.main(["serve", "--workspace", str(tmp_path)])

    assert result == EXIT_SUCCESS
    assert seen["approval_mode"] is None
    assert serve_entry.call_args.kwargs["workspace"] == tmp_path


@pytest.mark.parametrize(
    ("extra_args", "expected_port", "expected_open_browser"),
    [
        ((), None, True),
        (("--port", "8012"), 8012, True),
        (("--port", "8012", "--no-open"), 8012, False),
    ],
)
def test_web_forwards_launcher_options(
    extra_args: tuple[str, ...],
    expected_port: int | None,
    expected_open_browser: bool,
    tmp_path: Path,
) -> None:
    with recorded_config_loader() as seen:
        with patch.object(SERVER_COMMAND_MODULE, "web", autospec=True) as web_entry:
            result = CLI_MODULE.main(
                [
                    "web",
                    "--workspace",
                    str(tmp_path),
                    "--host",
                    "127.0.0.1",
                    *extra_args,
                ]
            )

    assert result == EXIT_SUCCESS
    assert seen["approval_mode"] is None
    assert web_entry.call_args.kwargs == {
        "workspace": tmp_path,
        "host": "127.0.0.1",
        "port": expected_port,
        "config": seen["config"],
        "open_browser": expected_open_browser,
    }


def test_tui_runs_the_textual_app_with_workspace_and_approval_mode(tmp_path: Path) -> None:
    tui_module = importlib.import_module("voidcode.tui")

    with patch.object(tui_module, "VoidCodeTUI", autospec=True) as tui_class:
        result = CLI_MODULE.main(
            [
                "tui",
                "--workspace",
                str(tmp_path),
                "--approval-mode",
                "ask",
            ]
        )

    assert result == EXIT_SUCCESS
    tui_class.assert_called_once_with(workspace=tmp_path, approval_mode="ask")
    tui_class.return_value.run.assert_called_once_with()


def test_tui_without_approval_mode_passes_none(tmp_path: Path) -> None:
    tui_module = importlib.import_module("voidcode.tui")

    with patch.object(tui_module, "VoidCodeTUI", autospec=True) as tui_class:
        result = CLI_MODULE.main(["tui", "--workspace", str(tmp_path)])

    assert result == EXIT_SUCCESS
    tui_class.assert_called_once_with(workspace=tmp_path, approval_mode=None)
