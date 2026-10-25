"""Serve the core HTTP transport on loopback."""

from __future__ import annotations

import secrets
import socket
from pathlib import Path

from .http import create_core_app
from .static import frontend_distribution


def generate_bearer_token() -> str:
    return secrets.token_urlsafe(32)


def serve_core(
    *,
    workspace: Path,
    state_dir: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    bearer_token: str | None = None,
    open_browser: bool = False,
    quiet: bool = False,
    **_kwargs: object,
) -> None:
    import uvicorn

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("transport binds loopback only")
    token = bearer_token or generate_bearer_token()
    frontend_dist = frontend_distribution()
    app = create_core_app(
        workspace=workspace,
        state_dir=state_dir,
        bearer_token=token,
        frontend_dist=frontend_dist,
    )
    sock = None
    actual_port = port
    if port == 0:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        actual_port = sock.getsockname()[1]

    endpoint = f"http://{host}:{actual_port}"
    if not quiet:
        print(f"core endpoint: {endpoint}", flush=True)
        print(f"core bearer token: {token}", flush=True)
        print(f"core ready: {endpoint}", flush=True)
        if frontend_dist is not None:
            print(f"web client: {endpoint}/", flush=True)
    if open_browser and frontend_dist is not None:
        import webbrowser

        webbrowser.open(f"{endpoint}/")

    config = uvicorn.Config(app, host=host, port=actual_port, log_level="warning", timeout_graceful_shutdown=10)
    server = uvicorn.Server(config)
    if sock is not None:
        server.run(sockets=[sock])
    else:
        server.run()
