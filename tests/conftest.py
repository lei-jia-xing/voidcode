from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.core_factory import close_cores

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="voidcode-pytest-config-")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="voidcode-pytest-appdata-")


@pytest.fixture(autouse=True)
def _isolated_xdg_runtime_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test isolation for runtime state, cache, and data directories.

    The SQLite database, provider catalog cache, and exported user data default
    to user-global locations (XDG or LOCALAPPDATA on Windows). Per-test roots
    prevent sharing and corruption. XDG_CONFIG_HOME and APPDATA are intentionally
    session-scoped at module import time because runtime config loaders cache them.
    The inherited VOIDCODE_DB_PATH is cleared so it cannot bypass these roots;
    tests may still set an explicit override through their own monkeypatch.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".xdg-cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".xdg-data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / ".local-app-data"))
    monkeypatch.delenv("VOIDCODE_DB_PATH", raising=False)


@pytest.fixture(autouse=True)
def _close_core_factory() -> Iterator[None]:
    """Close every Core opened through ``open_core_for_test`` after each test."""
    yield from close_cores()


# ---------------------------------------------------------------------------
# Offline guard: no test may reach the outside world by accident.
#
# A connect to an unroutable host burns the full timeout per call (and a routed
# one makes the assertion depend on the host), so a real non-loopback connect
# fails immediately instead. Loopback stays open: transport and loopback-provider
# tests run real local servers. A test whose subject *is* the live network opts
# out with ``allow_live_network``. Plugin code runs in the out-of-process plugin
# host, so this guard covers the test and core processes only; a plugin's own
# network use is the plugin's declared capability, exercised with loopback
# endpoints in tests.
# ---------------------------------------------------------------------------

_OPT_OUT_FIXTURE = "allow_live_network"


@pytest.fixture
def allow_live_network() -> None:
    """Opt a test out of the autouse offline guard.

    Requesting this fixture lets the test open real non-loopback sockets. Only a
    test whose subject is the real network may ask for it; everything else stays
    deterministic.
    """
    return None


def _is_loopback_address(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    if host in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _deny_live_sockets(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail immediately on a real non-loopback connect.

    Loopback stays open: the HTTP transport tests run a real server. Unix sockets
    stay open too (their address is a path, not a host). ``SSLSocket`` overrides
    ``connect``, so it is guarded separately; both delegate the loopback case back
    to the real implementation.
    """
    if _OPT_OUT_FIXTURE in request.fixturenames:
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> Any:
        if self.family != socket.AF_UNIX and not _is_loopback_address(address):
            raise OSError(f"tests must not open a real network connection: {address!r}")
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> Any:
        if self.family != socket.AF_UNIX and not _is_loopback_address(address):
            raise OSError(f"tests must not open a real network connection: {address!r}")
        return real_connect_ex(self, address, *args, **kwargs)

    for socket_class in (socket.socket, ssl.SSLSocket):
        monkeypatch.setattr(socket_class, "connect", guarded_connect)
        monkeypatch.setattr(socket_class, "connect_ex", guarded_connect_ex)
