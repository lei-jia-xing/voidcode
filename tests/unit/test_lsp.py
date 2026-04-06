from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from voidcode.runtime.config import RuntimeLspConfig


def _load_lsp_symbols() -> tuple[Any, Any]:
    module: Any = import_module("voidcode.runtime.lsp")
    return module.DisabledLspManager, module.LspConfigState


DisabledLspManager: Any
LspConfigState: Any
DisabledLspManager, LspConfigState = _load_lsp_symbols()


def test_lsp_config_state_defaults_to_disabled_with_no_servers() -> None:
    state = LspConfigState.from_runtime_config(None)

    assert state.configured_enabled is False
    assert state.servers == {}
    assert state.resolve("pyright") is None


def test_lsp_config_state_wraps_runtime_lsp_config_servers() -> None:
    state = LspConfigState.from_runtime_config(
        RuntimeLspConfig(
            enabled=True,
            servers={
                "pyright": {"command": ["pyright-langserver", "--stdio"]},
                "ruff": {"command": ["ruff", "server"]},
            },
        )
    )

    pyright_server = state.resolve("pyright")

    assert state.configured_enabled is True
    assert tuple(state.servers) == ("pyright", "ruff")
    assert pyright_server is not None
    assert pyright_server.definition == {"command": ["pyright-langserver", "--stdio"]}
    assert state.resolve("missing") is None


def test_disabled_lsp_manager_reports_configured_servers_without_runtime_availability() -> None:
    manager = DisabledLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": {"command": ["pyright-langserver", "--stdio"]}},
        )
    )

    state = manager.current_state()

    assert manager.configuration.configured_enabled is True
    assert manager.resolve("pyright") == manager.configuration.resolve("pyright")
    assert state.mode == "disabled"
    assert state.configuration is manager.configuration
    assert tuple(state.servers) == ("pyright",)
    assert state.servers["pyright"].configured is True
    assert state.servers["pyright"].available is False
