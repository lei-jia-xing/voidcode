from __future__ import annotations

import importlib
import socket
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch


def _noop_uvicorn_run(app: object, *, host: str, port: int, lifespan: str) -> None:
    _ = (app, host, port, lifespan)


def _noop_uvicorn_run_with_fd(
    app: object, *, host: str, port: int, lifespan: str, fd: int | None = None
) -> None:
    _ = (app, host, port, lifespan, fd)


def _write_frontend_dist_fixture(tmp_path: Path) -> Path:
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return frontend_dist


@contextmanager
def _frontend_dist_override(frontend_dist: Path) -> Any:
    yield frontend_dist


class _PackagedFrontendDistStub:
    def __init__(self, *, is_dir: bool, has_index: bool) -> None:
        self._is_dir = is_dir
        self._has_index = has_index

    def is_dir(self) -> bool:
        return self._is_dir

    def joinpath(self, name: str) -> _PackagedFrontendDistStub:
        if name == "index.html":
            return _PackagedFrontendDistStub(is_dir=False, has_index=self._has_index)
        return _PackagedFrontendDistStub(is_dir=False, has_index=False)

    def is_file(self) -> bool:
        return self._has_index


def test_serve_forwards_runtime_config_to_http_app_factory() -> None:
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/server-workspace")
    config = cast(Any, SimpleNamespace(approval_mode="allow"))

    with patch.object(
        server, "create_runtime_app", autospec=True, return_value=object()
    ) as app_mock:
        with patch("importlib.import_module", autospec=True) as import_module_mock:
            uvicorn = SimpleNamespace(run=_noop_uvicorn_run)
            import_module_mock.return_value = uvicorn
            server.serve(workspace=workspace, host="127.0.0.1", port=8001, config=config)

    app_mock.assert_called_once_with(workspace=workspace, config=config, frontend_dist=None)


def test_serve_delegates_to_shared_runtime_server() -> None:
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/server-workspace")
    config = cast(Any, SimpleNamespace(approval_mode="allow"))

    with patch.object(server, "_run_runtime_server", autospec=True) as run_mock:
        server.serve(workspace=workspace, host="127.0.0.1", port=8001, config=config)

    run_mock.assert_called_once_with(
        workspace=workspace,
        host="127.0.0.1",
        port=8001,
        config=config,
    )


def test_web_delegates_to_shared_runtime_server(tmp_path: Path) -> None:
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/server-workspace")
    config = cast(Any, SimpleNamespace(approval_mode="allow"))
    frontend_dist = _write_frontend_dist_fixture(tmp_path)

    with patch.object(server, "_run_runtime_server", autospec=True) as run_mock:
        with patch.object(server, "_frontend_dist_context", autospec=True) as frontend_context_mock:
            frontend_context_mock.return_value = _frontend_dist_override(frontend_dist)
            server.web(
                workspace=workspace,
                host="127.0.0.1",
                port=8001,
                config=config,
                open_browser=False,
            )

    run_mock.assert_called_once_with(
        workspace=workspace,
        host="127.0.0.1",
        port=8001,
        config=config,
        frontend_dist=frontend_dist,
        listener_socket=None,
    )


def test_web_selects_ephemeral_port_when_unspecified(tmp_path: Path) -> None:
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/server-workspace")
    config = cast(Any, SimpleNamespace(approval_mode="allow"))
    frontend_dist = _write_frontend_dist_fixture(tmp_path)
    listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener_socket.bind(("127.0.0.1", 0))

    try:
        expected_port = cast(int, listener_socket.getsockname()[1])
        with patch.object(server, "_run_runtime_server", autospec=True) as run_mock:
            with patch.object(
                server, "_frontend_dist_context", autospec=True
            ) as frontend_context_mock:
                frontend_context_mock.return_value = _frontend_dist_override(frontend_dist)
                with patch.object(
                    server,
                    "_reserve_listener_socket",
                    autospec=True,
                    return_value=listener_socket,
                ):
                    server.web(
                        workspace=workspace,
                        host="127.0.0.1",
                        port=None,
                        config=config,
                        open_browser=False,
                    )

        run_mock.assert_called_once_with(
            workspace=workspace,
            host="127.0.0.1",
            port=expected_port,
            config=config,
            frontend_dist=frontend_dist,
            listener_socket=listener_socket,
        )
    finally:
        listener_socket.close()


def test_reserve_listener_socket_uses_ipv6_socket_family(monkeypatch: Any) -> None:
    server = importlib.import_module("voidcode.server")
    socket_module = importlib.import_module("socket")
    socket_calls: list[tuple[int, int, int]] = []
    bind_calls: list[object] = []

    class _FakeSocket:
        def __init__(self, family: int, socket_type: int, proto: int) -> None:
            socket_calls.append((family, socket_type, proto))
            self._sockname = ("::1", 41234, 0, 0)

        def bind(self, sockaddr: object) -> None:
            bind_calls.append(sockaddr)

        def getsockname(self) -> tuple[str, int, int, int]:
            return self._sockname

    monkeypatch.setattr(
        server.socket,
        "getaddrinfo",
        lambda host, port, family, type, proto, flags: [
            (
                socket_module.AF_INET6,
                socket_module.SOCK_STREAM,
                socket_module.IPPROTO_TCP,
                "",
                (host, port, 0, 0),
            )
        ],
    )
    monkeypatch.setattr(server.socket, "socket", _FakeSocket)

    listener_socket = server._reserve_listener_socket("::1")

    assert listener_socket.getsockname() == ("::1", 41234, 0, 0)
    assert socket_calls == [
        (socket_module.AF_INET6, socket_module.SOCK_STREAM, socket_module.IPPROTO_TCP)
    ]
    assert bind_calls == [("::1", 0, 0, 0)]


def test_run_runtime_server_passes_reserved_socket_fd() -> None:
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/server-workspace")
    config = cast(Any, SimpleNamespace(approval_mode="allow"))
    listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener_socket.bind(("127.0.0.1", 0))

    try:
        with patch.object(
            server, "create_runtime_app", autospec=True, return_value=object()
        ) as app_mock:
            with patch("importlib.import_module", autospec=True) as import_module_mock:
                uvicorn = SimpleNamespace(run=_noop_uvicorn_run_with_fd)
                import_module_mock.return_value = uvicorn
                server._run_runtime_server(
                    workspace=workspace,
                    host="127.0.0.1",
                    port=cast(int, listener_socket.getsockname()[1]),
                    config=config,
                    listener_socket=listener_socket,
                )

        app_mock.assert_called_once_with(workspace=workspace, config=config, frontend_dist=None)
        assert listener_socket.fileno() == -1
    finally:
        if listener_socket.fileno() != -1:
            listener_socket.close()


def test_web_closes_reserved_listener_when_frontend_setup_fails() -> None:
    server = importlib.import_module("voidcode.server")
    listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener_socket.bind(("127.0.0.1", 0))

    try:
        with patch.object(
            server,
            "_reserve_listener_socket",
            autospec=True,
            return_value=listener_socket,
        ):
            with patch.object(
                server, "_frontend_dist_context", autospec=True
            ) as frontend_context_mock:
                frontend_context_mock.side_effect = SystemExit(
                    "error: frontend web bundle not found"
                )
                try:
                    server.web(workspace=Path("/tmp/server-workspace"), host="127.0.0.1", port=None)
                except SystemExit as exc:
                    assert str(exc) == "error: frontend web bundle not found"
                else:
                    raise AssertionError("expected frontend setup failure")

        assert listener_socket.fileno() == -1
    finally:
        if listener_socket.fileno() != -1:
            listener_socket.close()


def test_frontend_dist_context_prefers_repo_dist_over_packaged_fallback(tmp_path: Path) -> None:
    server = importlib.import_module("voidcode.server")
    repo_frontend_dist = _write_frontend_dist_fixture(tmp_path)
    packaged_frontend_dist = tmp_path / "packaged-dist"
    packaged_frontend_dist.mkdir()
    (packaged_frontend_dist / "index.html").write_text("packaged", encoding="utf-8")

    with patch.object(server, "_REPO_FRONTEND_DIST", repo_frontend_dist):
        with patch.object(
            server,
            "_packaged_frontend_dist",
            autospec=True,
            return_value=_PackagedFrontendDistStub(is_dir=True, has_index=True),
        ):
            with patch.object(server.importlib_resources, "as_file", autospec=True) as as_file_mock:
                with server._frontend_dist_context() as frontend_dist:
                    assert frontend_dist == repo_frontend_dist

    as_file_mock.assert_not_called()
