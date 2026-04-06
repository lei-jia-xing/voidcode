from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _noop_uvicorn_run(app: object, *, host: str, port: int, lifespan: str) -> None:
    _ = (app, host, port, lifespan)


def test_serve_forwards_runtime_config_to_http_app_factory() -> None:
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/server-workspace")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(
        server, "create_runtime_app", autospec=True, return_value=object()
    ) as app_mock:
        with patch("importlib.import_module", autospec=True) as import_module_mock:
            uvicorn = SimpleNamespace(run=_noop_uvicorn_run)
            import_module_mock.return_value = uvicorn
            server.serve(workspace=workspace, host="127.0.0.1", port=8001, config=config)

    app_mock.assert_called_once_with(workspace=workspace, config=config)
