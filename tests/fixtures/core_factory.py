"""Shared Core lifecycle and loopback-provider scaffolding for tests.

Tests that drive a real Core against a scripted model endpoint all repeat the
same four steps: script a loopback server, write the workspace's
``voidcode.toml``, open the Core over a per-test state directory, and close it
afterward. These helpers keep that contract in one place; a test keeps only the
parts that are specific to what it proves.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

from tests.provider_loopback import OpenAILoopbackServer
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core

_CREATED: list[Core] = []


@contextmanager
def loopback_server(replies: list[dict[str, object]] | None = None) -> Generator[OpenAILoopbackServer]:
    """Serve ``replies`` on a daemon thread and stop it on exit."""
    server = OpenAILoopbackServer(replies if replies is not None else [])
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def register_core(core: Core) -> Core:
    """Register an already-opened Core for the autouse ``close_cores`` teardown."""
    _CREATED.append(core)
    return core


def discard_core(core: Core) -> None:
    """Stop tracking a Core the test closed itself, avoiding a double close."""
    if core in _CREATED:
        _CREATED.remove(core)


def open_core_for_test(tmp_path: Path) -> Core:
    """Open a Core at ``tmp_path/workspace`` with state in ``tmp_path/state``.

    The workspace is created when absent, so a test with no ``voidcode.toml``
    can use the helper directly. The Core is registered for the autouse
    ``close_cores`` teardown, so a test that fails mid-turn still releases its
    processes and database handles.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def write_loopback_config(
    workspace: Path,
    server: OpenAILoopbackServer,
    source: Path,
    *,
    plugin_id: str,
    read_only: bool = False,
    enabled: bool = True,
    api_key: str = "loopback-secret-not-for-storage",
) -> None:
    """Write the ``voidcode.toml`` for a guardrailed test workspace.

    The ``endpoint`` provider points at ``server``; bundled file tools are
    disabled so only ``plugin_id`` (a path plugin at ``source``) contributes
    capabilities. Policy is yolo unless ``read_only`` asks for the restricted
    stance.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": read_only},
        "providers": {
            "endpoint": {
                "api_key": api_key,
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {"files": {"enabled": False}, plugin_id: {"path": str(source), "enabled": enabled}},
    }
    (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")


def close_cores() -> Iterator[None]:
    """Fixture body closing every Core registered by ``open_core_for_test``."""
    try:
        yield
    finally:
        while _CREATED:
            core = _CREATED.pop()
            if not core.closed:
                core.close()
