"""MCP runtime plumbing: the owned event loop, SDK contexts, and connection teardown.

This module is composed into the `ManagedMcpManager` facade. It holds the
serve/submit primitives and the per-server connection lifecycle, but no
startup, discovery, dispatch, or snapshot policy.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, TypeVar

import httpx2
from mcp import ClientSession
from mcp.shared.exceptions import MCPError

from .types import (
    McpCachedToolSurface,
    McpCallStage,
    McpConfigState,
    McpManagerState,
    McpServerRuntimeState,
    McpServerStatus,
    McpToolDescriptor,
)

if TYPE_CHECKING:
    from ._shutdown import _ShutdownSnapshot

logger = logging.getLogger("voidcode.plugins.mcp.mcp_manager")
_MCP_REQUEST_TIMEOUT_CODES = frozenset({-32001, 408})
#: The SDK's CONNECTION_CLOSED JSON-RPC error code (mcp_types.CONNECTION_CLOSED).
#: The streamable-http transport resolves an in-flight request with this error
#: when the underlying connection dies (e.g. the connect POST failed), which is
#: how an unreachable endpoint surfaces through ``ClientSession.initialize``.
_MCP_CONNECTION_CLOSED_CODE = -32000

_T = TypeVar("_T")


def _flatten_exception_group(exc: BaseExceptionGroup) -> BaseException:
    """Select the first leaf for diagnostics; callers preserve control exceptions."""
    leaf: BaseException = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]
    return leaf


def _is_connection_error(exc: BaseException) -> bool:
    """True for errors indicating the remote endpoint could not be reached.

    A dead remote-http endpoint surfaces either as an ``httpx2.ConnectError``
    (or ``httpx2.ConnectTimeout``) wrapped in an exception group, or -
    when the transport teardown resolves the in-flight initialize request - as
    an ``MCPError`` carrying the SDK's CONNECTION_CLOSED code.
    """
    if isinstance(exc, httpx2.ConnectError | httpx2.ConnectTimeout):
        return True
    if isinstance(exc, MCPError):
        return exc.code == _MCP_CONNECTION_CLOSED_CODE
    return False


def _is_discovery_connection_closed(exc: BaseException) -> bool:
    """True when a discovery call failed because the connection was dropped.

    The streamable-http transport resolves an in-flight request with the SDK's
    CONNECTION_CLOSED error (-32000) when the underlying connection dies (e.g.
    a Cloudflare-flapped remote-http endpoint). That is transient, so the
    runtime retries ``tools/list`` once with a fresh connection before failing.
    """
    return isinstance(exc, MCPError) and _is_connection_error(exc)


class _OwnedAsyncContext:
    """One SDK context whose enter and exit both run in a single dedicated loop task.

    The SDK transports require same-task exit (anyio cancel scopes), and the
    parked task absorbs scope cancellations from a collapsing transport, so
    SDK calls made from other tasks observe the real transport error instead
    of a cancellation.
    """

    def __init__(self, context: AbstractAsyncContextManager[Any]) -> None:
        self._context = context
        self._entered_event = asyncio.Event()
        self._exit_requested = asyncio.Event()
        self._exited_event = asyncio.Event()
        self._entered_ok = False
        self._value: Any = None
        self._enter_error: BaseException | None = None
        self._exit_error: BaseException | None = None
        self._exit_result: Any = None

    @property
    def entered(self) -> bool:
        return self._entered_ok

    async def enter(self) -> Any:
        asyncio.ensure_future(self._run())
        await self._entered_event.wait()
        if self._enter_error is not None:
            raise self._enter_error
        return self._value

    async def _run(self) -> None:
        try:
            value = await self._context.__aenter__()
        except BaseException as exc:
            self._enter_error = exc
            self._entered_event.set()
            return
        self._value = value
        self._entered_ok = True
        self._entered_event.set()
        try:
            # Parked mid-context until teardown. A collapsing transport may
            # cancel this wait; the exit below still runs in this same task.
            await self._exit_requested.wait()
        finally:
            try:
                self._exit_result = await self._context.__aexit__(None, None, None)
            except BaseException as exc:
                self._exit_error = exc
            finally:
                self._exited_event.set()

    async def exit(self) -> Any:
        if not self._entered_ok:
            return None
        self._exit_requested.set()
        await self._exited_event.wait()
        if self._exit_error is not None:
            raise self._exit_error
        return self._exit_result


class _RunningMcpServer:
    """One SDK session and its context-manager handles, owned by the manager's loop."""

    def __init__(
        self,
        *,
        server_name: str,
        transport_context: _OwnedAsyncContext,
        session_context: _OwnedAsyncContext,
        session: ClientSession,
        stderr_log: IO[str] | None,
    ) -> None:
        self.server_name = server_name
        self.transport_context = transport_context
        self.session_context = session_context
        self.session = session
        self.stderr_log = stderr_log
        # The Python SDK ClientSession is shared per configured server process.
        # Serialize list/call operations per server so concurrent loop tasks do
        # not interleave mutable SDK session state.
        self.io_lock = asyncio.Lock()


class _CallAttempt:
    """Thread-safe marker for whether a tools/call may have reached the server."""

    def __init__(self) -> None:
        self._started = threading.Event()

    @property
    def request_may_have_started(self) -> bool:
        return self._started.is_set()

    def mark_request_started(self) -> None:
        self._started.set()


class _ManagerState:
    """Shared manager state and cross-mixin contracts.

    The facade supplies these at runtime; declaring them on one base keeps each
    responsibility module independently type-checkable.
    """

    _configuration: McpConfigState
    _running_servers: dict[str, _RunningMcpServer]
    _server_states: dict[str, McpServerRuntimeState]
    _request_timeout_seconds: float
    _tool_descriptors_by_server: dict[str, dict[str, McpToolDescriptor]]
    _discovered_tools: dict[str, tuple[McpToolDescriptor, ...]]
    _startup_lock: asyncio.Lock
    _shutdown_called: bool
    _shutdown_snapshot: _ShutdownSnapshot | None
    _loop: asyncio.AbstractEventLoop
    _loop_thread: threading.Thread

    if TYPE_CHECKING:

        def _set_server_state_owned(self, *, key: str, status: McpServerStatus, error: str | None = None) -> None: ...

        def _message_for_exception(self, exc: BaseException, *, fallback: str) -> str: ...

        def _is_timeout_error(self, exc: BaseException) -> bool: ...

        async def _close_partial_server_owned(
            self,
            *,
            session_context: _OwnedAsyncContext | None,
            transport_context: _OwnedAsyncContext | None,
            stderr_log: IO[str] | None,
        ) -> None: ...

        async def _stop_running_server_owned(
            self,
            key: str,
            *,
            expected: _RunningMcpServer | None = None,
            timeout: float | None = None,
        ) -> None: ...

        async def _ensure_running_owned(self, *, server_name: str, workspace: Path) -> _RunningMcpServer: ...

        async def _list_tools_for_server_owned(self, *, server_name: str, workspace: Path) -> tuple[McpToolDescriptor, ...]: ...

        async def _call_sdk_owned(
            self,
            running: _RunningMcpServer,
            *,
            stage: McpCallStage,
            method: str,
            operation: Callable[[], Coroutine[Any, Any, _T]],
            suppress_failure_event_when: Callable[[Exception], bool] | None = None,
            attempt: _CallAttempt | None = None,
        ) -> _T: ...

        def _max_discovery_pages(self) -> int: ...

        def _teardown_timeout_seconds(self) -> float: ...


class _ServerRuntimeMixin(_ManagerState):
    """Serve the manager loop and own connection replacement/teardown."""

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run one coroutine on the owned loop and return its result (sync callers only)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _passive(self, make_coro: Callable[[], Coroutine[Any, Any, _T]], project: Callable[[_ShutdownSnapshot], _T]) -> _T:
        """Serve a passive state read from the loop, or from the shutdown snapshot afterwards."""
        snapshot = self._shutdown_snapshot
        if snapshot is not None:
            return project(snapshot)
        try:
            return self._submit(make_coro())
        except RuntimeError:
            snapshot = self._shutdown_snapshot
            if snapshot is None:
                raise
            return project(snapshot)

    async def _current_state_owned(self) -> McpManagerState:
        return McpManagerState(
            mode="managed",
            configuration=self._configuration,
            servers=dict(self._server_states),
        )

    async def _cached_surface_owned(self, *, workspace: Path) -> McpCachedToolSurface:
        _ = workspace
        return McpCachedToolSurface(servers=dict(self._discovered_tools))

    async def _descriptor_for_owned(self, *, server_name: str, tool_name: str) -> McpToolDescriptor | None:
        return self._tool_descriptors_by_server.get(server_name, {}).get(tool_name)

    def _set_server_state_owned(self, *, key: str, status: McpServerStatus, error: str | None = None) -> None:
        self._server_states[key] = McpServerRuntimeState(status=status, error=error)

    def _message_for_exception(self, exc: BaseException, *, fallback: str) -> str:
        if self._is_timeout_error(exc):
            return f"MCP server timed out after {self._request_timeout_seconds:.1f}s. The server may be unresponsive."
        if isinstance(exc, MCPError):
            return f"MCP server error: {exc}"
        return f"{fallback}: {exc}"

    @staticmethod
    def _is_timeout_error(exc: BaseException) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, MCPError):
            return exc.code in _MCP_REQUEST_TIMEOUT_CODES
        return False

    @staticmethod
    async def _close_partial_server_owned(
        *,
        session_context: _OwnedAsyncContext | None,
        transport_context: _OwnedAsyncContext | None,
        stderr_log: IO[str] | None,
    ) -> None:
        if session_context is not None:
            with suppress(BaseException):
                await session_context.exit()
        if transport_context is not None:
            with suppress(BaseException):
                await transport_context.exit()
        if stderr_log is not None:
            stderr_log.close()

    async def _stop_running_server_owned(
        self,
        key: str,
        *,
        expected: _RunningMcpServer | None = None,
        timeout: float | None = None,
    ) -> None:
        running = self._running_servers.get(key)
        if running is None or (expected is not None and running is not expected):
            return
        self._tool_descriptors_by_server.pop(key, None)
        self._running_servers.pop(key)
        try:
            if timeout is None:
                errors = await self._terminate_running_server_owned(running)
            else:
                errors = await asyncio.wait_for(self._terminate_running_server_owned(running), timeout)
        except TimeoutError:
            errors = (f"teardown timed out after {timeout or 0.0:.1f}s",)
        except Exception as exc:
            errors = (f"teardown failed: {type(exc).__name__}: {exc}",)
        if errors:
            message = f"MCP[{key}]: {'; '.join(errors)}"
            logger.warning(message)
            if key not in self._running_servers:
                self._set_server_state_owned(key=key, status="failed", error=message)
        elif key not in self._running_servers:
            self._set_server_state_owned(key=key, status="stopped")

    @staticmethod
    async def _terminate_running_server_owned(running: _RunningMcpServer) -> tuple[str, ...]:
        errors: list[str] = []
        for label, context in (("session", running.session_context), ("transport", running.transport_context)):
            try:
                await context.exit()
            except asyncio.CancelledError:
                raise
            except KeyboardInterrupt, SystemExit:
                raise
            except BaseException as exc:
                errors.append(f"{label} teardown failed: {type(exc).__name__}: {exc}")
        if running.stderr_log is not None:
            try:
                running.stderr_log.close()
            except KeyboardInterrupt, SystemExit:
                raise
            except BaseException as exc:
                errors.append(f"stderr log close failed: {type(exc).__name__}: {exc}")
        return tuple(errors)
