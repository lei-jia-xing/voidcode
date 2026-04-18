from __future__ import annotations

import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.request import url2pathname

import pytest
from lsprotocol import converters as lsp_converters
from lsprotocol import types as lsp_types

from voidcode.lsp import ResolvedLspServerConfig
from voidcode.runtime.config import RuntimeLspConfig, RuntimeLspServerConfig
from voidcode.tools import ToolCall


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


def test_lsp_config_state_matches_dockerls_for_canonical_dockerfile_name() -> None:
    state = LspConfigState.from_runtime_config(
        RuntimeLspConfig(
            enabled=True,
            servers={
                "yamlls": RuntimeLspServerConfig(command=("yaml-language-server", "--stdio")),
                "dockerls": RuntimeLspServerConfig(),
            },
        )
    )

    assert state.matching_servers(Path("Dockerfile")) == ("dockerls",)
    assert state.default_server_name(Path("Dockerfile")) == "dockerls"


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


def test_stop_running_server_cleans_up_after_shutdown_timeout(tmp_path: Path) -> None:
    manager = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    module = import_module("voidcode.runtime.lsp")
    running_server_cls = module._RunningLspServer

    class _FakeProcess:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            if self.terminated or self.killed:
                return 0
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.terminated = True

        def wait(self, timeout: float = 0.0) -> int:
            if self.terminated or self.killed:
                return 0
            raise subprocess.TimeoutExpired(cmd="fake-lsp", timeout=timeout)

    fake_process = _FakeProcess()
    server_config = manager.configuration.resolve("pyright")
    assert server_config is not None
    manager._running_servers["pyright"] = running_server_cls(
        config=server_config,
        process=fake_process,
        workspace_root=tmp_path,
        initialized=True,
    )

    def _raise_timeout(*args: object, **kwargs: object) -> object:
        raise TimeoutError("shutdown timed out")

    manager._send_request = _raise_timeout

    manager._stop_running_server("pyright", record_event=True)

    assert "pyright" not in manager._running_servers
    state = manager.current_state().servers["pyright"]
    assert state.status == "stopped"
    assert state.available is False
    assert fake_process.terminated is True
    stopped_event = manager.drain_events()[0]
    assert stopped_event.event_type == "runtime.lsp_server_stopped"
    assert stopped_event.payload["workspace_root"] == str(tmp_path)


def test_path_from_file_uri_preserves_unc_host() -> None:
    expected = Path(url2pathname("//server/share/project/main.py"))

    assert ManagedLspManager._path_from_file_uri("file://server/share/project/main.py") == expected


def test_lsp_operation_strings_match_protocol_method_names() -> None:
    tool_module = import_module("voidcode.tools.lsp")
    operation_enum = tool_module.LspOperation

    assert operation_enum.PREPARE_CALL_HIERARCHY.value == "textDocument/prepareCallHierarchy"
    assert operation_enum.INCOMING_CALLS.value == "callHierarchy/incomingCalls"
    assert operation_enum.OUTGOING_CALLS.value == "callHierarchy/outgoingCalls"


def test_initialize_params_from_lsprotocol_keep_existing_wire_shape(tmp_path: Path) -> None:
    converter = lsp_converters.get_converter()
    workspace_root = tmp_path.resolve()
    init_params = lsp_types.InitializeParams(
        process_id=123,
        client_info=lsp_types.ClientInfo(name="voidcode", version="0.1.0"),
        locale="zh-CN",
        root_uri=workspace_root.as_uri(),
        workspace_folders=[
            lsp_types.WorkspaceFolder(uri=workspace_root.as_uri(), name=workspace_root.name)
        ],
        capabilities=lsp_types.ClientCapabilities(),
    )

    payload = converter.unstructure(init_params, unstructure_as=lsp_types.InitializeParams)

    assert payload["processId"] == 123
    assert payload["clientInfo"] == {"name": "voidcode", "version": "0.1.0"}
    assert payload["locale"] == "zh-CN"
    assert payload["rootUri"] == workspace_root.as_uri()
    assert payload["workspaceFolders"] == [
        {"uri": workspace_root.as_uri(), "name": workspace_root.name}
    ]
    assert payload["capabilities"] == {}


def test_lsp_tool_builds_same_text_document_position_wire_shape(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")

    class _StubResponse:
        def __init__(self, response: dict[str, object]) -> None:
            self.response = response

    captured: dict[str, object] = {}

    def _requester(
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
    ) -> _StubResponse:
        captured["server_name"] = server_name
        captured["method"] = method
        captured["params"] = params
        captured["workspace"] = workspace
        return _StubResponse({"result": {"ok": True}})

    tool_module = import_module("voidcode.tools.lsp")
    tool = tool_module.LspTool(requester=_requester)

    result = tool.invoke(
        ToolCall(
            tool_name="lsp",
            arguments={
                "operation": "textDocument/definition",
                "filePath": "sample.py",
                "line": 2,
                "character": 3,
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert captured["server_name"] is None
    assert captured["method"] == "textDocument/definition"
    assert captured["workspace"] == tmp_path.resolve()
    assert captured["params"] == {
        "textDocument": {"uri": sample_file.resolve().as_uri()},
        "position": {"line": 1, "character": 2},
    }
