from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol, cast

from .runtime.http import create_runtime_app


class UvicornModule(Protocol):
    def run(self, app: object, *, host: str, port: int, lifespan: str) -> None: ...


def serve(*, workspace: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    app = create_runtime_app(workspace=workspace)
    uvicorn = cast(UvicornModule, importlib.import_module("uvicorn"))
    uvicorn.run(app, host=host, port=port, lifespan="off")
