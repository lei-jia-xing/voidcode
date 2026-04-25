from __future__ import annotations

import importlib
import webbrowser
from pathlib import Path
from typing import Protocol, cast

from .runtime.config import RuntimeConfig
from .runtime.http import create_runtime_app


class UvicornModule(Protocol):
    def run(self, app: object, *, host: str, port: int, lifespan: str) -> None: ...


def _run_runtime_server(
    *,
    workspace: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    config: RuntimeConfig | None = None,
    frontend_dist: Path | None = None,
) -> None:
    app = create_runtime_app(workspace=workspace, config=config, frontend_dist=frontend_dist)
    uvicorn = cast(UvicornModule, importlib.import_module("uvicorn"))
    uvicorn.run(app, host=host, port=port, lifespan="off")


def serve(
    *,
    workspace: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    config: RuntimeConfig | None = None,
) -> None:
    _run_runtime_server(workspace=workspace, host=host, port=port, config=config)


_BANNER = """\
  ╭─────────────────────────────────╮
  │          VoidCode Web           │
  │    Local-first Coding Agent     │
  ╰─────────────────────────────────╯
"""

# Locate the built frontend dist relative to this package.
# server.py lives at src/voidcode/server.py; the repo root is 3 levels up.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


def web(
    *,
    workspace: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    config: RuntimeConfig | None = None,
) -> None:
    url = f"http://{host}:{port}"

    print(_BANNER)
    print(f"  Local server running at: {url}")
    print()

    frontend_dist = _FRONTEND_DIST if _FRONTEND_DIST.is_dir() else None

    try:
        webbrowser.open(url)
    except Exception:
        pass

    _run_runtime_server(
        workspace=workspace,
        host=host,
        port=port,
        config=config,
        frontend_dist=frontend_dist,
    )
