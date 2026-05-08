from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch


def _noop_uvicorn_run(app: object, *, host: str, port: int, lifespan: str) -> None:
    _ = (app, host, port, lifespan)


def _write_frontend_dist_fixture(tmp_path: Path) -> Path:
    frontend_dist = tmp_path / "dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return frontend_dist


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
        with patch.object(server, "_FRONTEND_DIST", frontend_dist):
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
    )


def test_web_selects_ephemeral_port_when_unspecified(tmp_path: Path) -> None:
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/server-workspace")
    config = cast(Any, SimpleNamespace(approval_mode="allow"))
    frontend_dist = _write_frontend_dist_fixture(tmp_path)

    with patch.object(server, "_run_runtime_server", autospec=True) as run_mock:
        with patch.object(server, "_FRONTEND_DIST", frontend_dist):
            with patch.object(server, "_select_ephemeral_port", autospec=True, return_value=43123):
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
        port=43123,
        config=config,
        frontend_dist=frontend_dist,
    )


def test_select_ephemeral_port_uses_ipv6_socket_family(monkeypatch: Any) -> None:
    server = importlib.import_module("voidcode.server")
    socket_module = importlib.import_module("socket")
    socket_calls: list[tuple[int, int, int]] = []
    bind_calls: list[object] = []

    class _FakeSocket:
        def __init__(self, family: int, socket_type: int, proto: int) -> None:
            socket_calls.append((family, socket_type, proto))
            self._sockname = ("::1", 41234, 0, 0)

        def __enter__(self) -> _FakeSocket:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = (exc_type, exc, tb)
            return None

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

    port = server._select_ephemeral_port("::1")

    assert port == 41234
    assert socket_calls == [
        (socket_module.AF_INET6, socket_module.SOCK_STREAM, socket_module.IPPROTO_TCP)
    ]
    assert bind_calls == [("::1", 0, 0, 0)]
