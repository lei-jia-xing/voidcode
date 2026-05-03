from __future__ import annotations

import os
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any, cast
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
        module.LspMessageBoundsError,
        module.LspProtocolError,
        module.ManagedLspManager,
        module.MAX_LSP_MESSAGE_BYTES,
        module.LspRequest,
        module.build_lsp_manager,
    )


DisabledLspManager: Any
LspConfigState: Any
LspMessageBoundsError: Any
LspProtocolError: Any
ManagedLspManager: Any
max_lsp_message_bytes: int
LspRequest: Any
build_lsp_manager: Any
(
    DisabledLspManager,
    LspConfigState,
    LspMessageBoundsError,
    LspProtocolError,
    ManagedLspManager,
    max_lsp_message_bytes,
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


def test_lsp_config_state_enables_explicit_servers_when_enabled_omitted() -> None:
    state = LspConfigState.from_runtime_config(
        RuntimeLspConfig(
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))}
        )
    )

    assert state.configured_enabled is True
    assert tuple(state.servers) == ("pyright",)


def test_lsp_config_state_preserves_explicit_disable_with_servers() -> None:
    state = LspConfigState.from_runtime_config(
        RuntimeLspConfig(
            enabled=False,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )

    assert state.configured_enabled is False
    assert tuple(state.servers) == ("pyright",)


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


def test_build_lsp_manager_enables_when_servers_configured_without_explicit_flag() -> None:
    manager = build_lsp_manager(
        RuntimeLspConfig(
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))}
        )
    )

    state = manager.current_state()

    assert isinstance(manager, ManagedLspManager)
    assert state.mode == "managed"
    assert state.configuration.configured_enabled is True


def test_build_lsp_manager_respects_explicit_disable_even_with_servers() -> None:
    manager = build_lsp_manager(
        RuntimeLspConfig(
            enabled=False,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )

    state = manager.current_state()

    assert isinstance(manager, DisabledLspManager)
    assert state.mode == "disabled"
    assert state.configuration.configured_enabled is False


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


def test_build_lsp_manager_accepts_extended_builtin_catalog_entry() -> None:
    manager = build_lsp_manager(
        RuntimeLspConfig(
            enabled=True,
            servers={"clangd": RuntimeLspServerConfig()},
        )
    )

    assert isinstance(manager, ManagedLspManager)
    resolved = manager.configuration.resolve("clangd")
    assert resolved is not None
    assert resolved.command == ("clangd",)
    assert resolved.matches_path(Path("main.cpp")) is True


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

    with pytest.raises(ValueError, match="failed to start LSP server broken") as exc_info:
        _ = manager.request(request)

    state = manager.current_state()
    assert state.servers["broken"].status == "failed"
    assert state.servers["broken"].available is False
    assert state.servers["broken"].last_error is not None
    assert "installed and available on PATH" in str(exc_info.value)
    assert "lsp.servers.broken.command" in str(exc_info.value)


def test_read_message_rejects_oversized_content_length() -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(
            write_fd,
            f"Content-Length: {max_lsp_message_bytes + 1}\r\n\r\n".encode("ascii"),
        )

        class _FakeProcess:
            def __init__(self, stdout: Any) -> None:
                self.stdout = stdout

        with os.fdopen(read_fd, "rb", buffering=0) as stdout:
            process = _FakeProcess(stdout)
            with pytest.raises(LspMessageBoundsError, match="Content-Length"):
                _ = ManagedLspManager._read_message(process)
    finally:
        os.close(write_fd)


def test_read_message_rejects_invalid_json_payload() -> None:
    body = b"{invalid json"
    payload = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)

        class _FakeProcess:
            def __init__(self, stdout: Any) -> None:
                self.stdout = stdout

        with os.fdopen(read_fd, "rb", buffering=0) as stdout:
            process = _FakeProcess(stdout)
            with pytest.raises(LspProtocolError, match="invalid JSON"):
                _ = ManagedLspManager._read_message(process)
    finally:
        os.close(write_fd)


def test_read_message_rejects_non_object_json_payload() -> None:
    body = b"[]"
    payload = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, payload)

        class _FakeProcess:
            def __init__(self, stdout: Any) -> None:
                self.stdout = stdout

        with os.fdopen(read_fd, "rb", buffering=0) as stdout:
            process = _FakeProcess(stdout)
            with pytest.raises(LspProtocolError, match="JSON-RPC object"):
                _ = ManagedLspManager._read_message(process)
    finally:
        os.close(write_fd)


def test_send_request_matches_generated_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    module = import_module("voidcode.runtime.lsp")
    running_server_cls = module._RunningLspServer

    class _FakeProcess:
        pass

    fake_process = _FakeProcess()
    server_config = manager.configuration.resolve("pyright")
    assert server_config is not None
    running_server = running_server_cls(
        config=server_config,
        process=cast(subprocess.Popen[bytes], fake_process),
        workspace_root=Path("."),
    )

    sent_ids: list[int] = []
    responses: list[dict[str, object] | None] = [
        {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 3}},
        {"jsonrpc": "2.0", "id": 1, "result": {"value": "first"}},
        {"jsonrpc": "2.0", "id": 2, "result": {"value": "second"}},
    ]

    def _fake_write_message(*, process: object, message: dict[str, object]) -> None:
        _ = process
        message_id = message["id"]
        assert isinstance(message_id, int)
        sent_ids.append(message_id)

    def _fake_read_message(*, process: object, timeout: float = 30.0) -> dict[str, object] | None:
        _ = process, timeout
        return responses.pop(0)

    monkeypatch.setattr(manager, "_write_message", _fake_write_message)
    monkeypatch.setattr(manager, "_read_message", _fake_read_message)

    first = manager._send_request(
        running_server,
        method="textDocument/definition",
        params={},
        server_name="pyright",
    )
    second = manager._send_request(
        running_server,
        method="textDocument/references",
        params={},
        server_name="pyright",
    )

    assert sent_ids == [1, 2]
    assert first["result"] == {"value": "first"}
    assert second["result"] == {"value": "second"}


def test_managed_lsp_manager_terminates_process_when_initialize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    module = import_module("voidcode.runtime.lsp")

    class _FakeProcess:
        stdin = object()
        stdout = object()

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
            _ = timeout
            if self.terminated or self.killed:
                return 0
            raise subprocess.TimeoutExpired(cmd="fake-lsp", timeout=timeout)

    fake_process = _FakeProcess()

    def _fake_popen(
        command: list[str], *, cwd: Path, stdin: object, stdout: object, stderr: object
    ) -> _FakeProcess:
        _ = command, cwd, stdin, stdout, stderr
        return fake_process

    def _fail_initialize(*args: object, **kwargs: object) -> object:
        _ = args, kwargs
        raise ValueError("No response from LSP server pyright for initialize")

    def _ignore_notification(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    manager._send_request = _fail_initialize
    manager._send_notification = _ignore_notification
    monkeypatch.setattr(module.subprocess, "Popen", _fake_popen)
    request = LspRequest(
        server_name="pyright",
        method="textDocument/definition",
        params={"textDocument": {"uri": (tmp_path / "sample.py").as_uri()}},
        workspace=tmp_path,
    )

    with pytest.raises(ValueError, match="No response from LSP server pyright for initialize"):
        _ = manager.request(request)

    state = manager.current_state().servers["pyright"]
    assert state.status == "failed"
    assert state.available is False
    assert fake_process.terminated is True


def test_stop_running_server_cleans_up_after_shutdown_timeout(tmp_path: Path) -> None:
    manager = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    module = import_module("voidcode.runtime.lsp")
    running_server_cls = module._RunningLspServer
    lease_cls = module._LspServerLease
    lease_key_cls = module._LspRegistryKey
    registry = module._WORKSPACE_SCOPED_LSP_REGISTRY
    registry._entries.clear()
    registry._next_generation = 1

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
        process=cast(subprocess.Popen[bytes], fake_process),
        workspace_root=tmp_path,
        initialized=True,
    )
    lease_key = lease_key_cls(server_name="pyright", workspace_root=tmp_path)
    manager._leased_servers["pyright"] = lease_cls(key=lease_key, generation=1)
    registry._entries[lease_key] = module._SharedLspServerEntry(
        server=manager._running_servers["pyright"], generation=1, ref_count=1
    )

    def _raise_timeout(*_args: object, **_kwargs: object) -> object:
        raise TimeoutError("shutdown timed out")

    manager._send_request = _raise_timeout

    manager._release_server_lease("pyright", record_event=True, reason="shutdown")

    assert "pyright" not in manager._running_servers
    state = manager.current_state().servers["pyright"]
    assert state.status == "stopped"
    assert state.available is False
    assert fake_process.terminated is True
    stopped_event = manager.drain_events()[0]
    assert stopped_event.event_type == "runtime.lsp_server_stopped"
    assert stopped_event.payload["workspace_root"] == str(tmp_path)
    registry._entries.clear()
    registry._next_generation = 1


def test_managed_lsp_manager_reuses_workspace_scoped_server_across_runtime_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    module = import_module("voidcode.runtime.lsp")
    registry = module._WORKSPACE_SCOPED_LSP_REGISTRY
    registry._entries.clear()
    registry._next_generation = 1

    class _FakeProcess:
        stdin = object()
        stdout = object()

        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float = 0.0) -> int:
            _ = timeout
            self.terminated = True
            return 0

    popen_calls: list[tuple[list[str], Path]] = []

    def _fake_popen(
        command: list[str], *, cwd: Path, stdin: object, stdout: object, stderr: object
    ) -> _FakeProcess:
        _ = stdin, stdout, stderr
        popen_calls.append((command, cwd))
        return _FakeProcess()

    def _fake_send_request(
        self: Any,
        running_server: Any,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
    ) -> dict[str, object]:
        _ = self, running_server, params, server_name
        if method == "shutdown":
            return {"result": None}
        return {"result": {"ok": True}}

    monkeypatch.setattr(module.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(ManagedLspManager, "_send_request", _fake_send_request)

    def _fake_send_notification(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    monkeypatch.setattr(ManagedLspManager, "_send_notification", _fake_send_notification)

    manager_one = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    manager_two = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    request = LspRequest(
        server_name="pyright",
        method="textDocument/definition",
        params={"textDocument": {"uri": sample_file.as_uri()}},
        workspace=tmp_path,
    )

    first_response = manager_one.request(request)
    second_response = manager_two.request(request)

    assert first_response.response["result"] == {"ok": True}
    assert second_response.response["result"] == {"ok": True}
    assert popen_calls == [(["pyright-langserver", "--stdio"], tmp_path)]
    assert [event.event_type for event in manager_one.drain_events()] == [
        "runtime.lsp_server_started"
    ]
    assert [event.event_type for event in manager_two.drain_events()] == [
        "runtime.lsp_server_reused"
    ]

    assert manager_one.shutdown() == ()
    shutdown_events = manager_two.shutdown()
    assert [event.event_type for event in shutdown_events] == ["runtime.lsp_server_stopped"]
    assert shutdown_events[0].payload["reason"] == "shutdown"
    registry._entries.clear()
    registry._next_generation = 1


def test_managed_lsp_manager_rejects_reuse_when_shared_workspace_config_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    module = import_module("voidcode.runtime.lsp")
    registry = module._WORKSPACE_SCOPED_LSP_REGISTRY
    registry._entries.clear()
    registry._next_generation = 1

    class _FakeProcess:
        stdin = object()
        stdout = object()

        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float = 0.0) -> int:
            _ = timeout
            self.terminated = True
            return 0

    def _fake_popen(
        command: list[str], *, cwd: Path, stdin: object, stdout: object, stderr: object
    ) -> _FakeProcess:
        _ = command, cwd, stdin, stdout, stderr
        return _FakeProcess()

    def _fake_send_request(
        self: Any,
        running_server: Any,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
    ) -> dict[str, object]:
        _ = self, running_server, params, server_name
        if method == "shutdown":
            return {"result": None}
        return {"result": {"ok": True}}

    def _fake_send_notification(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    monkeypatch.setattr(module.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(ManagedLspManager, "_send_request", _fake_send_request)
    monkeypatch.setattr(ManagedLspManager, "_send_notification", _fake_send_notification)

    manager_one = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    manager_two = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("custom-pyright", "--stdio"))},
        )
    )
    request = LspRequest(
        server_name="pyright",
        method="textDocument/definition",
        params={"textDocument": {"uri": sample_file.as_uri()}},
        workspace=tmp_path,
    )

    _ = manager_one.request(request)

    with pytest.raises(ValueError, match="reuse rejected"):
        _ = manager_two.request(request)

    rejected_events = manager_two.drain_events()
    assert [event.event_type for event in rejected_events] == [
        "runtime.lsp_server_startup_rejected"
    ]
    assert rejected_events[0].payload["state"] == "rejected"
    assert manager_two.current_state().servers["pyright"].status == "failed"

    _ = manager_one.shutdown()
    registry._entries.clear()
    registry._next_generation = 1


def test_stale_shared_server_release_does_not_stop_replacement_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    module = import_module("voidcode.runtime.lsp")
    registry = module._WORKSPACE_SCOPED_LSP_REGISTRY
    registry._entries.clear()
    registry._next_generation = 1

    class _FakeProcess:
        stdin = object()
        stdout = object()

        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float = 0.0) -> int:
            _ = timeout
            self.terminated = True
            return 0

    popen_processes: list[_FakeProcess] = []

    def _fake_popen(
        command: list[str], *, cwd: Path, stdin: object, stdout: object, stderr: object
    ) -> _FakeProcess:
        _ = command, cwd, stdin, stdout, stderr
        process = _FakeProcess()
        popen_processes.append(process)
        return process

    def _fake_send_request(
        self: Any,
        running_server: Any,
        *,
        method: str,
        params: dict[str, object],
        server_name: str,
    ) -> dict[str, object]:
        _ = self, running_server, params, server_name
        if method == "shutdown":
            return {"result": None}
        return {"result": {"ok": True}}

    def _fake_send_notification(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    monkeypatch.setattr(module.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(ManagedLspManager, "_send_request", _fake_send_request)
    monkeypatch.setattr(ManagedLspManager, "_send_notification", _fake_send_notification)

    manager_one = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    manager_two = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    request = LspRequest(
        server_name="pyright",
        method="textDocument/definition",
        params={"textDocument": {"uri": sample_file.as_uri()}},
        workspace=tmp_path,
    )

    _ = manager_one.request(request)
    _ = manager_two.request(request)
    assert len(popen_processes) == 1
    assert [event.event_type for event in manager_one.drain_events()] == [
        "runtime.lsp_server_started"
    ]
    assert [event.event_type for event in manager_two.drain_events()] == [
        "runtime.lsp_server_reused"
    ]

    first_lease = manager_one._leased_servers["pyright"]
    first_entry = registry._entries[first_lease.key]
    assert first_entry.generation == 1
    first_entry.server.process.terminate()

    manager_three = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    _ = manager_three.request(request)
    assert len(popen_processes) == 2

    replacement_lease = manager_three._leased_servers["pyright"]
    replacement_entry = registry._entries[replacement_lease.key]
    assert replacement_lease.key == first_lease.key
    assert replacement_lease.generation == 2
    assert replacement_entry.generation == 2
    assert replacement_entry.server.process.poll() is None
    assert [event.event_type for event in manager_three.drain_events()] == [
        "runtime.lsp_server_started"
    ]

    assert manager_one.shutdown() == ()
    assert replacement_entry.server.process.poll() is None
    assert registry._entries[replacement_lease.key].generation == 2

    shutdown_events = manager_three.shutdown()
    assert [event.event_type for event in shutdown_events] == ["runtime.lsp_server_stopped"]
    assert popen_processes[1].terminated is True
    registry._entries.clear()
    registry._next_generation = 1


def test_managed_lsp_manager_shared_server_uses_single_request_id_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_file = tmp_path / "sample.py"
    sample_file.write_text("x = 1\n", encoding="utf-8")
    module = import_module("voidcode.runtime.lsp")
    registry = module._WORKSPACE_SCOPED_LSP_REGISTRY
    registry._entries.clear()
    registry._next_generation = 1

    class _FakeProcess:
        stdin = object()
        stdout = object()

        def __init__(self) -> None:
            self.terminated = False

        def poll(self) -> int | None:
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float = 0.0) -> int:
            _ = timeout
            self.terminated = True
            return 0

    popen_calls: list[tuple[list[str], Path]] = []
    sent_ids: list[int] = []
    responses: list[dict[str, object]] = [
        {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"ok": "one"}},
        {"jsonrpc": "2.0", "id": 3, "result": {"ok": "two"}},
        {"jsonrpc": "2.0", "id": 4, "result": None},
    ]

    def _fake_popen(
        command: list[str], *, cwd: Path, stdin: object, stdout: object, stderr: object
    ) -> _FakeProcess:
        _ = stdin, stdout, stderr
        popen_calls.append((command, cwd))
        return _FakeProcess()

    def _fake_write_message(*, process: object, message: dict[str, object]) -> None:
        _ = process
        message_id = message.get("id")
        if isinstance(message_id, int):
            sent_ids.append(message_id)

    def _fake_read_message(*, process: object, timeout: float = 30.0) -> dict[str, object] | None:
        _ = process, timeout
        if not responses:
            return None
        return responses.pop(0)

    def _fake_send_notification(*args: object, **kwargs: object) -> None:
        _ = args, kwargs

    monkeypatch.setattr(module.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(ManagedLspManager, "_write_message", staticmethod(_fake_write_message))
    monkeypatch.setattr(ManagedLspManager, "_read_message", staticmethod(_fake_read_message))
    monkeypatch.setattr(ManagedLspManager, "_send_notification", _fake_send_notification)

    manager_one = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    manager_two = ManagedLspManager(
        RuntimeLspConfig(
            enabled=True,
            servers={"pyright": RuntimeLspServerConfig(command=("pyright-langserver", "--stdio"))},
        )
    )
    request = LspRequest(
        server_name="pyright",
        method="textDocument/definition",
        params={"textDocument": {"uri": sample_file.as_uri()}},
        workspace=tmp_path,
    )

    first_response = manager_one.request(request)
    second_response = manager_two.request(request)

    assert first_response.response["result"] == {"ok": "one"}
    assert second_response.response["result"] == {"ok": "two"}
    assert popen_calls == [(["pyright-langserver", "--stdio"], tmp_path)]
    assert sent_ids == [1, 2, 3]

    assert [event.event_type for event in manager_one.drain_events()] == [
        "runtime.lsp_server_started"
    ]
    assert [event.event_type for event in manager_two.drain_events()] == [
        "runtime.lsp_server_reused"
    ]

    assert manager_one.shutdown() == ()
    shutdown_events = manager_two.shutdown()
    assert sent_ids == [1, 2, 3, 4]
    assert [event.event_type for event in shutdown_events] == ["runtime.lsp_server_stopped"]
    registry._entries.clear()
    registry._next_generation = 1


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


def test_lsp_tool_accepts_opencode_style_operation_aliases(tmp_path: Path) -> None:
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
                "operation": "goToDefinition",
                "filePath": "sample.py",
                "line": 1,
                "character": 1,
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert captured["method"] == "textDocument/definition"


def test_lsp_tool_accepts_workspace_symbol_query_without_position(tmp_path: Path) -> None:
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
                "operation": "workspaceSymbol",
                "filePath": "sample.py",
                "query": "Sample",
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert captured["method"] == "workspace/symbol"
    assert captured["workspace"] == tmp_path.resolve()
    assert captured["params"] == {"query": "Sample"}


def test_lsp_tool_accepts_document_symbol_without_position(tmp_path: Path) -> None:
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
        captured["method"] = method
        captured["params"] = params
        return _StubResponse({"result": {"ok": True}})

    tool_module = import_module("voidcode.tools.lsp")
    tool = tool_module.LspTool(requester=_requester)

    result = tool.invoke(
        ToolCall(
            tool_name="lsp",
            arguments={
                "operation": "documentSymbol",
                "filePath": "sample.py",
            },
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert captured["method"] == "textDocument/documentSymbol"
    assert captured["params"] == {
        "textDocument": {"uri": sample_file.resolve().as_uri()},
    }
