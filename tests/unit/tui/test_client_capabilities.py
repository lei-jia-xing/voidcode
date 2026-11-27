"""Client capability declarations, not transport identity, drive the TUI."""

from __future__ import annotations

import ast
import dataclasses
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from voidcode.sdk.client import (
    ClientCapabilities,
    ClientCapability,
    CommandCatalog,
    CommandResult,
    HealthModel,
    HealthStatus,
)
from voidcode.tui import rendering
from voidcode.tui.client import CoreRpcClient
from voidcode.tui.help_view import TUI_CONTROLS, render_help
from voidcode.tui.local_client import CoreLocalClient
from voidcode.tui.styles import Styles
from voidcode.tui.terminal_app import CoreTuiApp

_LOCAL = ClientCapabilities(frozenset({ClientCapability.EMBEDDED, ClientCapability.MODEL_SWITCH, ClientCapability.THINKING_SWITCH}))


class _Terminal:
    width = 120
    content_width = 120
    height = 30
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.frames: list[list[str]] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.rows))


class _Client:
    def __init__(self, capabilities: ClientCapabilities) -> None:
        self.capabilities = capabilities
        self.invoked: list[tuple[str, str, str]] = []

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(revision_id="rev-1", commands=())

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        expected_revision: str = "",
    ) -> CommandResult:
        self.invoked.append((session_id, name, expected_revision))
        return CommandResult(status="ok", data={"to_revision_id": "rev-2"})


def _health(workspace: str, *, levels: tuple[str, ...] = ("low", "high")) -> HealthStatus:
    return HealthStatus(
        status="ok",
        stage="serving",
        pid=1,
        workspace=workspace,
        revision_id="rev-1",
        launch_protocol=1,
        core_version="test",
        web_bundle=None,
        generation=1,
        active_upgrade=None,
        model=HealthModel(
            raw="openai/gpt",
            provider="openai",
            model="gpt",
            reasoning_effort="low",
            reasoning_levels=levels,
            context_window=200000,
        ),
    )


def _plain(rows: list[str]) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)


def _settle(app: CoreTuiApp, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if app._work.pending == 0 and app.event_queue.empty():
            return
        time.sleep(0.005)
    raise AssertionError("app background work never settled")


def test_client_arms_declare_their_capabilities() -> None:
    assert CoreLocalClient.capabilities == _LOCAL
    remote = CoreRpcClient("http://127.0.0.1:1")
    assert remote.capabilities == ClientCapabilities()
    try:
        remote.capabilities.supported = frozenset({ClientCapability.EMBEDDED})  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - a mutable declaration would be a contract break
        raise AssertionError("client capabilities must be frozen")


def test_gated_controls_name_the_capability_they_require() -> None:
    controls = {control.name: control for control in TUI_CONTROLS}
    assert controls["model"].requires is ClientCapability.MODEL_SWITCH
    assert controls["thinking"].requires is ClientCapability.THINKING_SWITCH
    assert all(control.requires is None for name, control in controls.items() if name not in {"model", "thinking"})


def test_footer_shortens_workspace_only_when_embedded_is_declared() -> None:
    workspace = str(Path.home() / "capability-workspace")
    embedded = CoreTuiApp(_Client(_LOCAL), session_id="s1", terminal=_Terminal())
    embedded.upgrade = _health(workspace)
    assert rendering.footer_context(embedded, 160).startswith("~/capability-workspace")

    remote = CoreTuiApp(_Client(ClientCapabilities()), session_id="s1", terminal=_Terminal())
    remote.upgrade = _health(workspace)
    assert workspace in rendering.footer_context(remote, 160)
    assert "~/" not in rendering.footer_context(remote, 160)


def test_remote_arm_refuses_model_and_thinking_switches_visibly(tmp_path: Path) -> None:
    terminal = _Terminal()
    client = _Client(ClientCapabilities())
    app = CoreTuiApp(client, session_id="s1", terminal=terminal, workspace_dir=tmp_path)
    app.upgrade = _health("/srv/ws")

    app._tui_model("openai/gpt")
    app._tui_thinking("high")

    error = "Model config lives with the server: switch from the server workspace"
    assert _plain(terminal.committed).count(error) == 2
    assert not (tmp_path / "voidcode.toml").exists()
    assert client.invoked == []
    assert app._work.pending == 0


def test_remote_arm_still_shows_the_admitted_model_and_levels() -> None:
    terminal = _Terminal()
    app = CoreTuiApp(_Client(ClientCapabilities()), session_id="s1", terminal=terminal)
    app.upgrade = _health("/srv/ws")

    app._tui_model("")
    app._tui_thinking("")

    text = _plain(terminal.committed)
    assert "=== Model (admitted) ===" in text
    assert "=== Thinking (admitted) ===" in text
    assert "openai/gpt" in text
    assert "low, high" in text


def test_declared_thinking_capability_switches_and_reloads(tmp_path: Path) -> None:
    terminal = _Terminal()
    client = _Client(_LOCAL)
    app = CoreTuiApp(client, session_id="s1", terminal=terminal, workspace_dir=tmp_path)
    app.upgrade = _health("/srv/ws")
    app.catalog_revision = "rev-1"

    app._tui_thinking("high")
    assert app.status_line == "Switching reasoning_effort..."
    _settle(app)

    import tomllib

    payload = tomllib.loads((tmp_path / "voidcode.toml").read_text(encoding="utf-8"))
    assert payload["model"]["reasoning_effort"] == "high"
    assert client.invoked == [("s1", "core.reload", "rev-1")]
    assert "Model switched: revision rev-2 admitted" in _plain(terminal.committed)


def test_help_marks_gated_controls_only_when_their_capability_is_missing() -> None:
    def rows(capabilities: ClientCapabilities) -> str:
        return "\n".join(
            render_help(
                (),
                revision="",
                width=120,
                styles=Styles(None),
                bindings={},
                conflicts={},
                quit_arm_seconds=1.5,
                capabilities=capabilities,
            )
        )

    remote = rows(ClientCapabilities())
    assert remote.count("(embedded only)") == 2
    assert "Show or switch the model. (embedded only)" in remote
    assert "Show or set the thinking level. (embedded only)" in remote
    assert "(embedded only)" not in rows(_LOCAL)


def test_tui_client_dispatch_has_no_transport_identity_checks() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "voidcode" / "tui"
    for name in ("terminal_app.py", "rendering.py", "help_view.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "CoreLocalClient" not in source, name
        assert "CoreRpcClient" not in source, name
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module in {"voidcode.tui.local_client", "voidcode.tui.client"}:
                pytest.fail(f"{name}:{node.lineno}: imports a concrete transport client")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance":
                assert not any(isinstance(argument, ast.Attribute) and argument.attr == "client" for argument in node.args), (
                    name,
                    node.lineno,
                )
