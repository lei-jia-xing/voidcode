from __future__ import annotations

import importlib
import socket
import webbrowser
from contextlib import closing
from pathlib import Path
from typing import Protocol, cast

from .runtime.config import RuntimeConfig
from .runtime.http import create_runtime_app


class UvicornModule(Protocol):
    def run(
        self,
        app: object,
        *,
        host: str,
        port: int,
        lifespan: str,
        fd: int | None = None,
    ) -> None: ...


def _run_runtime_server(
    *,
    workspace: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    config: RuntimeConfig | None = None,
    frontend_dist: Path | None = None,
    listener_socket: socket.socket | None = None,
) -> None:
    app = create_runtime_app(workspace=workspace, config=config, frontend_dist=frontend_dist)
    uvicorn = cast(UvicornModule, importlib.import_module("uvicorn"))
    if listener_socket is None:
        uvicorn.run(app, host=host, port=port, lifespan="off")
        return
    with closing(listener_socket):
        uvicorn.run(app, host=host, port=port, lifespan="off", fd=listener_socket.fileno())


def serve(
    *,
    workspace: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    config: RuntimeConfig | None = None,
) -> None:
    _run_runtime_server(workspace=workspace, host=host, port=port, config=config)


_BANNER = r"""\
            _     _               _
__   _____ (_) __| | ___ ___   __| | ___
\ \ / / _ \| |/ _` |/ __/ _ \ / _` |/ _ \
 \ V / (_) | | (_| | (_| (_) | (_| |  __/
  \_/ \___/|_|\__,_|\___\___/ \__,_|\___|

"""

# Locate the built frontend dist relative to this package.
# server.py lives at src/voidcode/server.py; the repo root is 3 levels up.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def _resolve_frontend_dist() -> Path:
    if not _FRONTEND_DIST.is_dir() or not (_FRONTEND_DIST / "index.html").is_file():
        raise SystemExit(
            "error: frontend web bundle not found. "
            "Run `mise run frontend:build` before `voidcode web`, "
            "or install a package that includes the built frontend assets."
        )
    return _FRONTEND_DIST


def _reserve_listener_socket(host: str) -> socket.socket:
    address_infos = socket.getaddrinfo(
        host,
        0,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
        flags=socket.AI_PASSIVE,
    )
    last_error: OSError | None = None
    for family, socket_type, proto, _canonname, sockaddr in address_infos:
        listener_socket: socket.socket | None = None
        try:
            listener_socket = socket.socket(family, socket_type, proto)
            listener_socket.bind(sockaddr)
            return listener_socket
        except OSError as exc:
            last_error = exc
            if listener_socket is not None:
                listener_socket.close()
            continue
    if last_error is not None:
        raise last_error
    msg = f"could not resolve local bind host for auto port selection: {host}"
    raise OSError(msg)


def web(
    *,
    workspace: Path,
    host: str = "127.0.0.1",
    port: int | None = None,
    config: RuntimeConfig | None = None,
    open_browser: bool = True,
) -> None:
    listener_socket = _reserve_listener_socket(host) if port is None else None
    try:
        selected_port: int
        if listener_socket is not None:
            selected_port = cast(int, listener_socket.getsockname()[1])
        else:
            selected_port = cast(int, port)
        url = f"http://{host}:{selected_port}"
        frontend_dist = _resolve_frontend_dist()

        print("VoidCode")
        print(_BANNER)
        print(f"  Local server running at: {url}")
        print()

        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        _run_runtime_server(
            workspace=workspace,
            host=host,
            port=selected_port,
            config=config,
            frontend_dist=frontend_dist,
            listener_socket=listener_socket,
        )
    except BaseException:
        if listener_socket is not None:
            listener_socket.close()
        raise
