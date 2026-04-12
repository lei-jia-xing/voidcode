from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from voidcode.lsp import ResolvedLspServerConfig
from voidcode.runtime.config import RuntimeLspConfig, RuntimeLspServerConfig


def _load_lsp_symbols() -> tuple[Any, ...]:
    module: Any = import_module("voidcode.runtime.lsp")
    return (
        module.DisabledLspManager,
        module.LspConfigState,
        module.ManagedLspManager,
        module.LspRequest,
        module.build_lsp_manager,
    )


DisabledLspManager: Any
LspConfigState: Any
ManagedLspManager: Any
LspRequest: Any
build_lsp_manager: Any
(
    DisabledLspManager,
    LspConfigState,
    ManagedLspManager,
    LspRequest,
    build_lsp_manager,
) = _load_lsp_symbols()


def test_lsp_config_state_defaults_to_disabled_with_no_servers() -> None:
    state = LspConfigState.from_runtime_config(None)

    assert state.configured_enabled is False
    assert state.servers == {}
    assert state.resolve("pyright") is None
    assert state.default_server_name() is None


def test_lsp_config_state_wraps_runtime_lsp_config_servers() -> None:
    state = LspConfigState.from_runtime_config(
        RuntimeLspConfig(
            enabled=True,
            servers={
                "pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio")),
                "ruff": RuntimeLspServerConfig(command=("ruff", "server"), languages=("python",)),
            },
        )
    )

    pyright_server = state.resolve("pyright")

    assert state.configured_enabled is True
    assert tuple(state.servers) == ("pyright", "ruff")
    assert pyright_server == ResolvedLspServerConfig(
        id="pyright",
        preset="pyright",
        command=("pyright-langserver", "--stdio"),
        extensions=(".py", ".pyi"),
        languages=("python",),
        root_markers=("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git"),
    )
    assert state.default_server_name() == "pyright"
    assert state.default_server_name(Path("main.py")) == "pyright"
    assert state.resolve("missing") is None


def test_disabled_lsp_manager_reports_configured_servers_without_runtime_availability() -> None:
    manager = DisabledLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )

    state = manager.current_state()

    assert manager.configuration.configured_enabled is True
    assert manager.resolve("pyright") == manager.configuration.resolve("pyright")
    assert state.mode == "disabled"
    assert state.configuration is manager.configuration
    assert tuple(state.servers) == ("pyright",)
    assert state.servers["pyright"].configured is True
    assert state.servers["pyright"].status == "stopped"
    assert state.servers["pyright"].available is False


def test_lsp_config_state_matches_server_by_file_extension() -> None:
    state = LspConfigState.from_runtime_config(
        RuntimeLspConfig(
            enabled=True,
            servers={
                "pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio")),
                "gopls": RuntimeLspServerConfig(command=("gopls",)),
            },
        )
    )

    assert state.matching_servers(Path("main.py")) == ("pyright",)
    assert state.matching_servers(Path("main.go")) == ("gopls",)
    assert state.default_server_name(Path("main.go")) == "gopls"


def test_disabled_lsp_manager_rejects_requests() -> None:
    manager = DisabledLspManager()

    with pytest.raises(ValueError, match="disabled"):
        _ = manager.request(
            LspRequest(
                server_name=None,
                method="textDocument/definition",
                params={},
                workspace=Path("."),
            )
        )


def test_build_lsp_manager_returns_managed_manager_when_enabled_and_configured() -> None:
    manager = build_lsp_manager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )

    state = manager.current_state()

    assert isinstance(manager, ManagedLspManager)
    assert state.mode == "managed"
    assert state.servers["pyright"].status == "stopped"


def test_build_lsp_manager_accepts_builtin_preset_without_explicit_command() -> None:
    manager = build_lsp_manager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig()},
        )
    )

    assert isinstance(manager, ManagedLspManager)
    assert manager.configuration.resolve("pyright") is not None
    assert manager.configuration.resolve("pyright").command == ("pyright-langserver", "--stdio")


def test_managed_lsp_manager_marks_failed_startup_when_command_is_missing(tmp_path: Path) -> None:
    manager = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={
                "broken": RuntimeLspServerConfig(command=("definitely-not-a-real-lsp-binary",))
            },
        )
    )

    request = LspRequest(
        server_name="broken",
        method="textDocument/definition",
        params={"textDocument": {"uri": (tmp_path / "sample.py").as_uri()}},
        workspace=tmp_path,
    )

    with pytest.raises(ValueError, match="failed to start LSP server broken"):
        _ = manager.request(request)

    state = manager.current_state()
    assert state.servers["broken"].status == "failed"
    assert state.servers["broken"].available is False
    assert state.servers["broken"].last_error is not None


def test_lsp_operation_strings_match_protocol_method_names() -> None:
    tool_module = import_module("voidcode.tools.lsp")
    operation_enum = tool_module.LspOperation

    assert operation_enum.PREPARE_CALL_HIERARCHY.value == "textDocument/prepareCallHierarchy"
    assert operation_enum.INCOMING_CALLS.value == "callHierarchy/incomingCalls"
    assert operation_enum.OUTGOING_CALLS.value == "callHierarchy/outgoingCalls"
