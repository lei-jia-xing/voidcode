"""Old MCP calls and cleanup cannot dispatch through or retire replacements."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

import voidcode.plugins.mcp.mcp_manager as mcp_manager_module
from voidcode.plugins.mcp import _mcp_config
from voidcode.plugins.mcp.mcp_manager import ManagedMcpManager


def _fake_running(server_name="demo"):
    return SimpleNamespace(server_name=server_name, io_lock=asyncio.Lock(), session=SimpleNamespace())


def _tool(name, schema):
    from mcp.types import Tool

    return Tool.model_validate({"name": name, "inputSchema": schema})


def _paged_manager(monkeypatch, session):
    manager = ManagedMcpManager(_mcp_config({"servers": {"demo": {"command": ["unused"]}}}))
    running = SimpleNamespace(server_name="demo", io_lock=asyncio.Lock(), session=session)

    async def _install():
        manager._running_servers["demo"] = running
        manager._set_server_state_owned(key="demo", status="running")

    manager._submit(_install())

    async def _ensure(**kwargs):
        _ = kwargs
        return running

    async def _terminate(running):
        _ = running
        return ()

    monkeypatch.setattr(manager, "_ensure_running_owned", _ensure)
    monkeypatch.setattr(manager, "_terminate_running_server_owned", _terminate)
    return manager


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


def test_discovery_reads_every_page_and_caches_complete_surface(monkeypatch, tmp_path):
    pages = {
        None: ([_tool("first", {"type": "object"})], "page-2"),
        "page-2": ([_tool("second", {"type": "object"})], None),
    }
    cursors = []

    class Session:
        async def list_tools(self, *, params=None):
            cursor = None if params is None else params.cursor
            cursors.append(cursor)
            tools, next_cursor = pages[cursor]
            return SimpleNamespace(tools=tools, next_cursor=next_cursor)

    manager = _paged_manager(monkeypatch, Session())
    try:
        tools = manager.list_tools(workspace=tmp_path)
        assert [tool.tool_name for tool in tools] == ["first", "second"]
        assert [tool.tool_name for tool in manager.cached_surface(workspace=tmp_path).servers["demo"]] == ["first", "second"]
        assert cursors == [None, "page-2"]
    finally:
        manager.shutdown()


def test_page_two_tool_is_schema_validated_before_dispatch(monkeypatch, tmp_path):
    calls = []

    class Session:
        async def list_tools(self, *, params=None):
            cursor = None if params is None else params.cursor
            if cursor is None:
                return SimpleNamespace(tools=[_tool("first", {"type": "object"})], next_cursor="page-2")
            return SimpleNamespace(
                tools=[
                    _tool(
                        "second",
                        {
                            "type": "object",
                            "properties": {"count": {"type": "integer"}},
                            "required": ["count"],
                            "additionalProperties": False,
                        },
                    )
                ],
                next_cursor=None,
            )

        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, dict(arguments), kwargs))
            raise AssertionError("invalid page-2 arguments must not reach the server")

    manager = _paged_manager(monkeypatch, Session())
    try:
        manager.list_tools(workspace=tmp_path)
        with pytest.raises(ValueError, match=r"MCP\[demo/second\]"):
            manager.call_tool(server_name="demo", tool_name="second", arguments={}, workspace=tmp_path)
        assert calls == []
    finally:
        manager.shutdown()


def test_unknown_tool_is_rejected_after_refresh(monkeypatch, tmp_path):
    calls = []

    class Session:
        async def list_tools(self, *, params=None):
            _ = params
            return SimpleNamespace(tools=[_tool("known", {"type": "object"})], next_cursor=None)

        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, dict(arguments), kwargs))
            raise AssertionError("undiscovered tool must not reach the server")

    manager = _paged_manager(monkeypatch, Session())
    try:
        manager.list_tools(workspace=tmp_path)
        with pytest.raises(ValueError, match="tool not discovered"):
            manager.call_tool(server_name="demo", tool_name="missing", arguments={}, workspace=tmp_path)
        assert calls == []
    finally:
        manager.shutdown()


def test_repeated_discovery_cursor_fails_visibly(monkeypatch, tmp_path):
    class Session:
        async def list_tools(self, *, params=None):
            _ = params
            return SimpleNamespace(tools=[_tool("loop", {"type": "object"})], next_cursor="again")

    manager = _paged_manager(monkeypatch, Session())
    try:
        with pytest.raises(ValueError, match="repeated cursor"):
            manager.list_tools(workspace=tmp_path)
        assert manager.cached_surface(workspace=tmp_path).servers == {}
    finally:
        manager.shutdown()


def test_discovery_page_cap_fails_visibly(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_manager_module, "_MAX_DISCOVERY_PAGES", 2)

    class Session:
        def __init__(self):
            self.page = 0

        async def list_tools(self, *, params=None):
            _ = params
            self.page += 1
            return SimpleNamespace(tools=[_tool(f"tool-{self.page}", {"type": "object"})], next_cursor=f"page-{self.page}")

    manager = _paged_manager(monkeypatch, Session())
    try:
        with pytest.raises(ValueError, match="exceeded 2 pages"):
            manager.list_tools(workspace=tmp_path)
        assert manager.cached_surface(workspace=tmp_path).servers == {}
    finally:
        manager.shutdown()


def test_raw_remote_tool_name_is_used_for_dispatch(monkeypatch, tmp_path):
    raw_name = "raw\nremote name"
    calls = []

    class Session:
        async def list_tools(self, *, params=None):
            _ = params
            return SimpleNamespace(tools=[_tool(raw_name, {"type": "object"})], next_cursor=None)

        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, dict(arguments), kwargs))
            return SimpleNamespace(content=[], structured_content=None, is_error=False)

    manager = _paged_manager(monkeypatch, Session())
    try:
        manager.list_tools(workspace=tmp_path)
        result = manager.call_tool(server_name="demo", tool_name=raw_name, arguments={}, workspace=tmp_path)
        assert result.content == []
        assert calls and calls[0][0] == raw_name
    finally:
        manager.shutdown()


def test_server_state_is_starting_until_initialize_completes(monkeypatch, tmp_path):
    entered = Event()
    release = Event()

    class Context:
        async def exit(self):
            return None

    class Session:
        async def initialize(self):
            entered.set()
            await asyncio.to_thread(release.wait)

    async def _open(**kwargs):
        _ = kwargs
        return Context(), Context(), Session(), None

    manager = ManagedMcpManager(_mcp_config({"servers": {"demo": {"command": ["unused"]}}}))
    monkeypatch.setattr(manager, "_open_session_owned", _open)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(manager._submit, manager._ensure_running_owned(server_name="demo", workspace=tmp_path))
            assert entered.wait(timeout=5)
            assert manager.current_state().servers["demo"].status == "starting"
            release.set()
            future.result(timeout=5)
        assert manager.current_state().servers["demo"].status == "running"
    finally:
        release.set()
        manager.shutdown()


def test_shutdown_stops_other_servers_when_one_teardown_hangs(monkeypatch):
    monkeypatch.setattr(mcp_manager_module, "_SERVER_TEARDOWN_TIMEOUT_SECONDS", 0.05)
    started = []
    finished = []

    class Context:
        def __init__(self, label, hang):
            self.label = label
            self.hang = hang

        async def exit(self):
            started.append(self.label)
            if self.hang:
                await asyncio.Event().wait()
            finished.append(self.label)

    manager = ManagedMcpManager(_mcp_config({"servers": {"hang": {"command": ["unused"]}, "ok": {"command": ["unused"]}}}))
    manager._running_servers["hang"] = SimpleNamespace(
        server_name="hang",
        session_context=Context("hang-session", True),
        transport_context=Context("hang-transport", False),
        stderr_log=None,
    )
    manager._running_servers["ok"] = SimpleNamespace(
        server_name="ok",
        session_context=Context("ok-session", False),
        transport_context=Context("ok-transport", False),
        stderr_log=None,
    )
    manager._set_server_state_owned(key="hang", status="running")
    manager._set_server_state_owned(key="ok", status="running")

    manager.shutdown()

    state = manager.current_state()
    assert "ok-session" in finished
    assert "ok-transport" in finished
    assert state.servers["ok"].status == "stopped"
    assert state.servers["hang"].status == "failed"
    assert "timed out" in (state.servers["hang"].error or "")
