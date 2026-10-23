"""Concurrent server reuse and stale-generation lease isolation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from voidcode.plugins.lsp.contracts import ResolvedLspServerConfig
from voidcode.plugins.lsp.lsp_manager import _LspRegistryKey, _RunningLspServer, _SessionScopedLspRegistry


def test_concurrent_acquire_starts_once_and_stale_leases_leave_replacement_intact(tmp_path: Path):
    registry = _SessionScopedLspRegistry()
    key = _LspRegistryKey("session", "echo", tmp_path)
    config = ResolvedLspServerConfig(id="echo", command=("echo-server",))
    started = []
    barrier = Barrier(2)

    def start(process_id):
        server = _RunningLspServer(config=config, process_id=process_id, workspace_root=tmp_path)
        started.append(server)
        return server

    def acquire():
        barrier.wait(timeout=5)
        return registry.acquire(key=key, config=config, start=start)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result(timeout=5) for future in [pool.submit(acquire), pool.submit(acquire)]]
    assert first[0] is second[0]
    assert len(started) == 1
    assert {first[2], second[2]} == {"started", "reused"}
    assert registry.release(lease=first[1]) is None
    assert registry.invalidate(key=key, process_id=first[0].process_id) is first[0]

    replacement, lease, _ = registry.acquire(key=key, config=config, start=start)
    assert replacement is not first[0]
    assert registry.release(lease=second[1]) is None
    assert registry.invalidate(key=key, process_id=first[0].process_id) is None
    assert registry.release(lease=lease) is replacement


def test_configuration_mismatch_does_not_acquire_a_reference(tmp_path: Path):
    registry = _SessionScopedLspRegistry()
    key = _LspRegistryKey("session", "echo", tmp_path)
    config = ResolvedLspServerConfig(id="echo", command=("echo-server",))
    server = _RunningLspServer(config=config, process_id="server", workspace_root=tmp_path)
    _, lease, _ = registry.acquire(key=key, config=config, start=lambda _: server)
    with pytest.raises(ValueError, match="different resolved configuration"):
        registry.acquire(key=key, config=replace(config, command=("other-server",)), start=lambda _: server)
    assert registry.release(lease=lease) is server
