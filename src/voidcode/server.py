from __future__ import annotations

import importlib
import importlib.resources as importlib_resources
import socket
import webbrowser
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Protocol, cast

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
_PACKAGED_FRONTEND_DIST = "_web_dist"
_REPO_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def _packaged_frontend_dist() -> object | None:
    package_root = importlib_resources.files("voidcode")
    frontend_dist = package_root.joinpath(_PACKAGED_FRONTEND_DIST)
    if frontend_dist.is_dir() and frontend_dist.joinpath("index.html").is_file():
        return frontend_dist
    return None


@contextmanager
def _frontend_dist_context() -> Iterator[Path]:
    if _REPO_FRONTEND_DIST.is_dir() and (_REPO_FRONTEND_DIST / "index.html").is_file():
        yield _REPO_FRONTEND_DIST
        return
    packaged_frontend_dist = _packaged_frontend_dist()
    if packaged_frontend_dist is not None:
        packaged_traversable = cast(Any, packaged_frontend_dist)
        with importlib_resources.as_file(packaged_traversable) as frontend_dist:
            yield frontend_dist
        return
    raise SystemExit(
        "error: frontend web bundle not found. "
        "Run `mise run frontend:build` before `voidcode web` in a source checkout, "
        "or install a package that includes the built frontend assets."
    )


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
        with _frontend_dist_context() as frontend_dist:
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
