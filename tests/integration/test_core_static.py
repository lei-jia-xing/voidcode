"""Static web bundle contract: the core transport serves the client, /api stays the API.

The cutover deleted the legacy web launcher; `voidcode serve` now serves the built bundle
itself. These tests drive a real listening server and assert the two halves that matter:
the client is reachable without a token (a browser cannot send one for a page load) while
every /api path keeps the bearer gate, and no request can read a file outside the bundle.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from voidcode.config import dumps_toml
from voidcode.transport.http import create_core_app

_TOKEN = "static-serving-token"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_bundle(root: Path) -> Path:
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>voidcode</title><div id='root'>spa entry</div>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('core client')\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    return root


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.toml").write_text(dumps_toml({"schema_version": 1}), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    return workspace, state


@contextmanager
def _listening(workspace: Path, state: Path, frontend_dist: Path) -> Iterator[str]:
    app = create_core_app(workspace=workspace, state_dir=state, bearer_token=_TOKEN, frontend_dist=frontend_dist)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    startup_failure: list[BaseException] = []

    def _run() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 - reported below instead of a bare timeout
            startup_failure.append(exc)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 60
    base = f"http://127.0.0.1:{port}"
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError as exc:
            if startup_failure or time.monotonic() > deadline:
                raise RuntimeError(f"core transport never opened its port: {startup_failure or 'timeout'}") from exc
            time.sleep(0.05)
    try:
        yield base
    finally:
        server.should_exit = True
        worker.join(timeout=10)


def _request(base: str, path: str, *, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(base + path, method="GET")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_bundle_is_public_and_api_keeps_its_token(tmp_path: Path) -> None:
    workspace, state = _workspace(tmp_path)
    bundle = _write_bundle(tmp_path / "dist")
    with _listening(workspace, state, bundle) as base:
        status, body = _request(base, "/")
        assert status == 200
        assert b"spa entry" in body

        # A client-side route is not a file: it still gets the entry document.
        status, body = _request(base, "/sessions/abc/anything")
        assert status == 200
        assert b"spa entry" in body

        # A real asset is served as itself.
        status, body = _request(base, "/assets/app.js")
        assert status == 200
        assert b"core client" in body

        # The page load cannot present a token; the API still demands one.
        status, _body = _request(base, "/api/health")
        assert status == 401
        status, body = _request(base, "/api/health", token=_TOKEN)
        assert status == 200
        assert json.loads(body)["revision_id"]

        # An unknown API path is an API 404, never the SPA document.
        status, body = _request(base, "/api/does-not-exist", token=_TOKEN)
        assert status == 404
        assert b"spa entry" not in body
        assert json.loads(body)["code"] == "not_found"


def test_bundle_never_reads_outside_its_root(tmp_path: Path) -> None:
    workspace, state = _workspace(tmp_path)
    bundle = _write_bundle(tmp_path / "dist")
    secret = tmp_path / "pyproject.toml"
    secret.write_text("[project]\nname = 'secret-outside-bundle'\n", encoding="utf-8")
    with _listening(workspace, state, bundle) as base:
        for path in ("/../pyproject.toml", "/%2e%2e/pyproject.toml", "/%2e%2e/%2e%2e/pyproject.toml", "//pyproject.toml"):
            status, body = _request(base, path)
            assert status == 200, path
            assert b"secret-outside-bundle" not in body, path
            assert b"spa entry" in body, path


def test_static_missing_asset_returns_404_and_api_cors_preflight_works_with_bearer_token(tmp_path: Path) -> None:
    workspace, state = _workspace(tmp_path)
    bundle = _write_bundle(tmp_path / "dist")
    with _listening(workspace, state, bundle) as base:
        status, body = _request(base, "/assets/missing.js")
        assert status == 404
        assert json.loads(body)["code"] == "not_found"

        preflight = urllib.request.Request(base + "/api/health", method="OPTIONS")
        preflight.add_header("Origin", "http://127.0.0.1:5173")
        with urllib.request.urlopen(preflight, timeout=30) as response:
            assert response.status == 204
            assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"

        unauthorized = urllib.request.Request(base + "/api/health", method="GET")
        unauthorized.add_header("Origin", "http://127.0.0.1:5173")
        try:
            urllib.request.urlopen(unauthorized, timeout=30)
            raise AssertionError("expected HTTP 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            assert exc.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
