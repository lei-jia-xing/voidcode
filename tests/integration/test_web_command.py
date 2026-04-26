"""Integration tests for the voidcode web launcher command.

These tests verify the user-facing launcher UX that sits on top of the shared
runtime server primitive. The happy path (banner + URL + delegation) and the
browser-open failure fallback are both covered here. The underlying server
startup (_run_runtime_server) is mocked throughout because it is a blocking
call tested separately in test_server.py.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch


def write_frontend_dist_fixture(tmp_path: Path) -> Path:
    frontend_dist = tmp_path / "dist"
    assets_dir = frontend_dist / "assets"
    assets_dir.mkdir(parents=True)
    (frontend_dist / "index.html").write_text(
        "<!DOCTYPE html><html><head><title>VoidCode</title></head><body></body></html>",
        encoding="utf-8",
    )
    (frontend_dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    return frontend_dist


def test_web_prints_banner_and_url(capsys: Any) -> None:
    """Verify the web command prints the banner and a usable local URL."""
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/web-banner-test")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(server, "_run_runtime_server", autospec=True):
        server.web(workspace=workspace, host="127.0.0.1", port=8080, config=config)

    captured = capsys.readouterr()
    assert "VoidCode" in captured.out
    assert "http://127.0.0.1:8080" in captured.out


def test_web_browser_open_failure_does_not_crash(capsys: Any) -> None:
    """Verify graceful degradation when webbrowser.open raises."""
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/web-browser-fail")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(server, "_run_runtime_server", autospec=True):
        with patch.object(server, "webbrowser", autospec=True) as webbrowser_mock:
            webbrowser_mock.open.side_effect = RuntimeError("no browser available")
            server.web(workspace=workspace, host="127.0.0.1", port=8080, config=config)

    captured = capsys.readouterr()
    assert "VoidCode" in captured.out
    assert "http://127.0.0.1:8080" in captured.out


def test_web_browser_open_gracefully_handles_false_return() -> None:
    """Verify graceful degradation when webbrowser.open returns False."""
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/web-browser-false")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(server, "_run_runtime_server", autospec=True):
        with patch.object(server, "webbrowser", autospec=True) as webbrowser_mock:
            webbrowser_mock.open.return_value = False
            server.web(workspace=workspace, host="127.0.0.1", port=8080, config=config)


def test_web_delegates_to_shared_runtime_server() -> None:
    """Verify the web command delegates to the runtime server primitive."""
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/web-delegate-test")
    config = SimpleNamespace(approval_mode="allow")
    expected_frontend_dist = server._FRONTEND_DIST if server._FRONTEND_DIST.is_dir() else None

    with patch.object(server, "_run_runtime_server", autospec=True) as run_mock:
        server.web(workspace=workspace, host="127.0.0.1", port=8001, config=config)

    run_mock.assert_called_once_with(
        workspace=workspace,
        host="127.0.0.1",
        port=8001,
        config=config,
        frontend_dist=expected_frontend_dist,
    )


def test_serve_remains_headless_and_uses_shared_runtime_server() -> None:
    """Verify the headless serve command does not inject frontend_dist."""
    server = importlib.import_module("voidcode.server")
    workspace = Path("/tmp/serve-headless-test")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(server, "_run_runtime_server", autospec=True) as run_mock:
        server.serve(workspace=workspace, host="127.0.0.1", port=9000, config=config)

    run_mock.assert_called_once_with(
        workspace=workspace,
        host="127.0.0.1",
        port=9000,
        config=config,
    )


def test_frontend_root_returns_html_when_dist_configured(tmp_path: Path) -> None:
    """Verify that GET / returns the frontend HTML when frontend_dist is set."""
    http_module = importlib.import_module("voidcode.runtime.http")
    rt_app = http_module.RuntimeTransportApp(
        runtime_factory=Mock(),
        frontend_dist=write_frontend_dist_fixture(tmp_path),
    )

    messages: list[dict[str, object]] = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {"type": "http", "method": "GET", "path": "/"}
    asyncio.run(rt_app(scope, receive, send))

    start = cast_start(sent)
    assert start["status"] == 200
    headers = decode_headers(start)
    assert "text/html" in headers.get("content-type", "")
    body = b"".join(
        cast(bytes, m.get("body", b""))
        for m in sent
        if cast(str, m["type"]) == "http.response.body"
    )
    assert b"<!DOCTYPE html>" in body
    assert b"VoidCode" in body


def test_frontend_serves_static_assets_when_dist_configured(tmp_path: Path) -> None:
    """Verify that static assets under /assets/ are served correctly."""
    http_module = importlib.import_module("voidcode.runtime.http")
    rt_app = http_module.RuntimeTransportApp(
        runtime_factory=Mock(),
        frontend_dist=write_frontend_dist_fixture(tmp_path),
    )

    messages: list[dict[str, object]] = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {"type": "http", "method": "GET", "path": "/favicon.svg"}
    asyncio.run(rt_app(scope, receive, send))

    start = cast_start(sent)
    assert start["status"] == 200
    body = b"".join(
        cast(bytes, m.get("body", b""))
        for m in sent
        if cast(str, m["type"]) == "http.response.body"
    )
    assert len(body) > 0


def test_frontend_does_not_spa_fallback_unknown_api_routes(tmp_path: Path) -> None:
    """Verify unknown API paths keep JSON 404 semantics with frontend_dist set."""
    http_module = importlib.import_module("voidcode.runtime.http")
    rt_app = http_module.RuntimeTransportApp(
        runtime_factory=Mock(),
        frontend_dist=write_frontend_dist_fixture(tmp_path),
    )

    messages: list[dict[str, object]] = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {"type": "http", "method": "GET", "path": "/api/not-real"}
    asyncio.run(rt_app(scope, receive, send))

    start = cast_start(sent)
    assert start["status"] == 404
    headers = decode_headers(start)
    assert "application/json" in headers.get("content-type", "")
    body = b"".join(
        cast(bytes, m.get("body", b""))
        for m in sent
        if cast(str, m["type"]) == "http.response.body"
    )
    assert json.loads(body.decode("utf-8")) == {"error": "not found"}


def test_frontend_returns_404_when_no_dist_configured() -> None:
    """Verify that GET / returns 404 when frontend_dist is None."""
    http_module = importlib.import_module("voidcode.runtime.http")
    rt_app = http_module.RuntimeTransportApp(
        runtime_factory=Mock(),
        frontend_dist=None,
    )

    messages: list[dict[str, object]] = [
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {"type": "http", "method": "GET", "path": "/"}
    asyncio.run(rt_app(scope, receive, send))

    start = cast_start(sent)
    assert start["status"] == 404


def cast_start(sent: list[dict[str, object]]) -> dict[str, object]:
    for m in sent:
        if cast(str, m["type"]) == "http.response.start":
            return m
    raise AssertionError("no http.response.start message")


def decode_headers(start: dict[str, object]) -> dict[str, str]:
    raw_headers = cast(list[tuple[bytes, bytes]], start.get("headers", []))
    return {k.decode("utf-8").lower(): v.decode("utf-8") for k, v in raw_headers}
