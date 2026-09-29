from __future__ import annotations

import importlib
import ipaddress
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="voidcode-pytest-config-")

# How long a leaked ``voidcode-background-task-*`` worker may take to finish its
# final durable writes before the test is declared to have left it running.
_BACKGROUND_WORKER_JOIN_TIMEOUT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def _join_leaked_background_workers(_isolated_xdg_runtime_dirs: None) -> Iterator[None]:
    """Join any background-task worker a test left running before isolation ends.

    Depends on ``_isolated_xdg_runtime_dirs`` so the join runs *first* on
    teardown, while the test's own XDG/database override is still in place: a
    worker still running its final durable writes then finalizes against its own
    temp database instead of the developer's.

    Many tests construct a runtime, dispatch a delegated task, and assert on its
    terminal state without closing the runtime (``runtime.__exit__`` is what
    joins background-task workers). The worker is a daemon and may still be
    running when the test body returns, so left alone it outlives the test and
    the environment the test ran under.

    The bounded join keeps this deterministic and fails loudly, naming the
    offending threads, if a worker cannot be stopped at all — that would be a
    real shutdown defect rather than test noise.
    """
    yield
    leaked = [thread for thread in threading.enumerate() if thread.name.startswith("voidcode-background-task-")]
    deadline = time.monotonic() + _BACKGROUND_WORKER_JOIN_TIMEOUT_SECONDS
    for thread in leaked:
        thread.join(timeout=max(deadline - time.monotonic(), 0.0))
    still_alive = sorted(thread.name for thread in leaked if thread.is_alive())
    assert not still_alive, f"test left background-task workers running after {_BACKGROUND_WORKER_JOIN_TIMEOUT_SECONDS}s: {still_alive}"


@pytest.fixture(autouse=True)
def _isolated_xdg_runtime_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-test isolation for XDG state/cache/data directories.

    The runtime SQLite database (XDG_STATE_HOME), provider catalog cache
    (XDG_CACHE_HOME), and exported user data (XDG_DATA_HOME) all default to
    user-global locations. Without per-test override, tests would share these
    files and corrupt each other. XDG_CONFIG_HOME is intentionally session-
    scoped at module import time because runtime config loaders cache it.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".xdg-cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".xdg-data"))


# ---------------------------------------------------------------------------
# Offline guards: no test may reach the outside world by accident.
#
# Three machine-dependent costs live behind "the real thing": a connect to an
# unroutable host burns the full timeout per call (and a routed one makes the
# assertion depend on the host), provider model discovery performs a live
# ``GET /v1/models``, and the doctor probes every formatter binary for its
# ``--version`` (``npx --version`` alone costs seconds when npm cannot reach its
# registry). Each guard fails or answers deterministically *and immediately*
# while keeping the observable contract the caller already sees on a failed
# network: model discovery still reports ``source="fallback"`` with
# ``last_refresh_status="failed"``, and the formatter check still reports the
# status it derives from ``shutil.which``.
#
# A test whose subject *is* the live network opts out with ``allow_live_network``.
# ---------------------------------------------------------------------------

_OPT_OUT_FIXTURE = "allow_live_network"


@pytest.fixture
def allow_live_network() -> None:
    """Opt a test out of the autouse offline guards.

    Requesting this fixture lets the test open real non-loopback sockets, use the
    live model-discovery fetcher, and launch the real external tool probes the
    doctor would run. Only a test whose subject is the real network may ask for
    it; everything else stays deterministic.
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


@pytest.fixture(autouse=True)
def _deny_live_model_discovery(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut the provider model-discovery HTTP call, offline and instantly.

    ``model_catalog`` performs discovery with the module-level ``urlopen``; that
    is the single seam every provider fetch routes through, and the one the
    discovery tests already stub per-test (their ``monkeypatch.setattr`` wins
    because it is applied after this fixture). Raising ``URLError`` reproduces
    exactly what an unreachable provider host already produces, so callers keep
    observing the same fallback result without waiting out the connect timeout.
    """
    if _OPT_OUT_FIXTURE in request.fixturenames:
        return

    catalog = importlib.import_module("voidcode.provider.model_catalog")

    def _offline_urlopen(http_request: Any, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise URLError(f"live model discovery is disabled in tests: {http_request.full_url}")

    monkeypatch.setattr(catalog, "urlopen", _offline_urlopen)


class _CannedSubprocess:
    """Stand-in for ``voidcode.doctor.checker``'s ``subprocess`` module.

    The doctor's executable checks only need the probe to succeed: the check's
    status comes from ``shutil.which``, and no test observes the tool's real
    version string. Keeping the launch out of the unit tests removes a
    machine-dependent external process (and its registry traffic).
    """

    TimeoutExpired = subprocess.TimeoutExpired
    CalledProcessError = subprocess.CalledProcessError

    @staticmethod
    def run(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _stub_external_tool_probes(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the doctor's ``<tool> --version`` probes without launching a tool."""
    if _OPT_OUT_FIXTURE in request.fixturenames:
        return
    checker = importlib.import_module("voidcode.doctor.checker")
    monkeypatch.setattr(checker, "subprocess", _CannedSubprocess)
