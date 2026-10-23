"""Old MCP calls and cleanup cannot dispatch through or retire replacements."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from voidcode.plugins.mcp import _mcp_config
from voidcode.plugins.mcp.mcp_manager import ManagedMcpManager


def setup_manager(monkeypatch):
    manager = ManagedMcpManager(_mcp_config({"servers": {"demo": {"command": ["unused"]}}}))
    old = SimpleNamespace(server_name="demo", call_lock=RLock(), session=SimpleNamespace())
    replacement = SimpleNamespace(server_name="demo", call_lock=RLock(), session=SimpleNamespace())
    manager._running_servers["demo"] = old
    manager._set_server_state(key="demo", status="running")
    monkeypatch.setattr(manager, "_ensure_portal", lambda: SimpleNamespace(call=lambda operation: operation()))
    terminated = []
    monkeypatch.setattr(manager, "_terminate_running_server", terminated.append)
    return manager, old, replacement, terminated


def test_failed_old_call_does_not_stop_or_poison_a_replacement(monkeypatch):
    manager, old, replacement, terminated = setup_manager(monkeypatch)
    entered, replaced = Event(), Event()

    def fail_after_replacement():
        entered.set()
        assert replaced.wait(timeout=5)
        raise RuntimeError("old connection failed")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager._call_sdk, old, stage="call", method="tools/call", operation=fail_after_replacement)
        assert entered.wait(timeout=5)
        with manager._state_lock:
            manager._running_servers["demo"] = replacement
        replaced.set()
        with pytest.raises(ValueError, match="old connection failed"):
            future.result(timeout=5)
    assert terminated == []
    assert manager._running_servers["demo"] is replacement
    assert manager.current_state().servers["demo"].status == "running"


def test_queued_stale_call_is_not_sent(monkeypatch):
    manager, old, replacement, terminated = setup_manager(monkeypatch)
    manager._running_servers["demo"] = replacement
    sent = []
    with pytest.raises(ValueError, match="request was not sent"):
        manager._call_sdk(old, stage="call", method="tools/call", operation=lambda: sent.append(True))
    assert sent == []
    assert terminated == []


def test_cleanup_finishing_after_restart_preserves_new_running_state(monkeypatch):
    manager, old, replacement, _ = setup_manager(monkeypatch)

    def restart_during_cleanup(running):
        assert running is old
        manager._running_servers["demo"] = replacement

    monkeypatch.setattr(manager, "_terminate_running_server", restart_during_cleanup)
    manager._stop_running_server("demo", expected=old)
    assert manager._running_servers["demo"] is replacement
    assert manager.current_state().servers["demo"].status == "running"


def test_old_discovery_response_cannot_replace_new_connection_inventory(monkeypatch, tmp_path):
    from mcp.types import ListToolsResult, Tool

    manager, old, replacement, _ = setup_manager(monkeypatch)
    marker = object()
    manager._tool_descriptors_by_server["demo"] = {"new": marker}
    manager._discovered_tools["demo"] = ()
    monkeypatch.setattr(manager, "_ensure_running", lambda **kwargs: old)

    def old_response(*args, **kwargs):
        manager._running_servers["demo"] = replacement
        return ListToolsResult(tools=[Tool.model_validate({"name": "old", "inputSchema": {"type": "object"}})])

    monkeypatch.setattr(manager, "_call_sdk", old_response)
    descriptors = manager._list_tools_for_server(server_name="demo", workspace=tmp_path)
    assert descriptors[0].tool_name == "old"
    assert manager.cached_surface(workspace=tmp_path).servers["demo"] == ()
    assert manager.descriptor_for(server_name="demo", tool_name="new") is marker
    assert manager.descriptor_for(server_name="demo", tool_name="old") is None


def test_startup_preserves_control_exception_groups(monkeypatch, tmp_path):
    manager = ManagedMcpManager(_mcp_config({"servers": {"demo": {"command": ["unused"]}}}))
    interrupt = BaseExceptionGroup("interrupted startup", [KeyboardInterrupt()])

    def interrupted_portal():
        raise interrupt

    monkeypatch.setattr(manager, "_ensure_portal", interrupted_portal)
    with pytest.raises(BaseExceptionGroup) as caught:
        manager._ensure_running(server_name="demo", workspace=tmp_path)
    assert caught.value is interrupt
    assert manager.current_state().servers["demo"].status == "stopped"
