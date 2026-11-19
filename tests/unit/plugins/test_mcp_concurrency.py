"""Old MCP calls and cleanup cannot dispatch through or retire replacements."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from voidcode.plugins.mcp import _mcp_config
from voidcode.plugins.mcp.mcp_manager import ManagedMcpManager


def _fake_running(server_name="demo"):
    return SimpleNamespace(server_name=server_name, io_lock=asyncio.Lock(), session=SimpleNamespace())


def setup_manager(monkeypatch):
    manager = ManagedMcpManager(_mcp_config({"servers": {"demo": {"command": ["unused"]}}}))
    old = _fake_running()
    replacement = _fake_running()

    async def _install():
        manager._running_servers["demo"] = old
        manager._set_server_state_owned(key="demo", status="running")

    manager._submit(_install())
    terminated = []

    async def _record_terminate(running):
        terminated.append(running)

    monkeypatch.setattr(manager, "_terminate_running_server_owned", _record_terminate)
    return manager, old, replacement, terminated


def test_failed_old_call_does_not_stop_or_poison_a_replacement(monkeypatch):
    manager, old, replacement, terminated = setup_manager(monkeypatch)
    try:
        entered = Event()
        gate = asyncio.Event()

        async def fail_after_replacement():
            entered.set()
            await gate.wait()
            raise RuntimeError("old connection failed")

        def _old_call():
            return manager._submit(manager._call_sdk_owned(old, stage="call", method="tools/call", operation=fail_after_replacement))

        async def _replace():
            manager._running_servers["demo"] = replacement
            manager._set_server_state_owned(key="demo", status="running")

        async def _current():
            return manager._running_servers["demo"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_old_call)
            assert entered.wait(timeout=5)
            manager._submit(_replace())
            manager._loop.call_soon_threadsafe(gate.set)
            with pytest.raises(ValueError, match="old connection failed"):
                future.result(timeout=5)
        assert terminated == []
        assert manager._submit(_current()) is replacement
        assert manager.current_state().servers["demo"].status == "running"
    finally:
        manager.shutdown()


def test_queued_stale_call_is_not_sent(monkeypatch):
    manager, old, replacement, terminated = setup_manager(monkeypatch)
    try:

        async def _replace():
            manager._running_servers["demo"] = replacement

        manager._submit(_replace())
        sent = []

        async def _send():
            sent.append(True)

        with pytest.raises(ValueError, match="request was not sent"):
            manager._submit(manager._call_sdk_owned(old, stage="call", method="tools/call", operation=_send))
        assert sent == []
        assert terminated == []
    finally:
        manager.shutdown()


def test_cleanup_finishing_after_restart_preserves_new_running_state(monkeypatch):
    manager, old, replacement, _ = setup_manager(monkeypatch)
    try:

        async def restart_during_cleanup(running):
            assert running is old
            manager._running_servers["demo"] = replacement
            manager._set_server_state_owned(key="demo", status="running")

        async def _current():
            return manager._running_servers["demo"]

        monkeypatch.setattr(manager, "_terminate_running_server_owned", restart_during_cleanup)
        manager._submit(manager._stop_running_server_owned("demo", expected=old))
        assert manager._submit(_current()) is replacement
        assert manager.current_state().servers["demo"].status == "running"
    finally:
        manager.shutdown()


def test_old_discovery_response_cannot_replace_new_connection_inventory(monkeypatch, tmp_path):
    from mcp.types import ListToolsResult, Tool

    manager, old, replacement, _ = setup_manager(monkeypatch)
    try:
        marker = object()

        async def _seed():
            manager._tool_descriptors_by_server["demo"] = {"new": marker}
            manager._discovered_tools["demo"] = ()

        manager._submit(_seed())

        async def _old_server(**kwargs):
            _ = kwargs
            return old

        async def old_response(*args, **kwargs):
            _ = args, kwargs
            manager._running_servers["demo"] = replacement
            return ListToolsResult(tools=[Tool.model_validate({"name": "old", "inputSchema": {"type": "object"}})])

        monkeypatch.setattr(manager, "_ensure_running_owned", _old_server)
        monkeypatch.setattr(manager, "_call_sdk_owned", old_response)
        descriptors = manager._submit(manager._list_tools_for_server_owned(server_name="demo", workspace=tmp_path))
        assert descriptors[0].tool_name == "old"
        assert manager.cached_surface(workspace=tmp_path).servers["demo"] == ()
        assert manager.descriptor_for(server_name="demo", tool_name="new") is marker
        assert manager.descriptor_for(server_name="demo", tool_name="old") is None
    finally:
        manager.shutdown()


def test_startup_preserves_control_exception_groups(monkeypatch, tmp_path):
    manager = ManagedMcpManager(_mcp_config({"servers": {"demo": {"command": ["unused"]}}}))
    try:
        interrupt = BaseExceptionGroup("interrupted startup", [KeyboardInterrupt()])

        async def interrupted_open(**kwargs):
            _ = kwargs
            raise interrupt

        monkeypatch.setattr(manager, "_open_session_owned", interrupted_open)
        with pytest.raises(BaseExceptionGroup) as caught:
            manager._submit(manager._ensure_running_owned(server_name="demo", workspace=tmp_path))
        assert caught.value is interrupt
        assert manager.current_state().servers["demo"].status == "stopped"
    finally:
        manager.shutdown()
