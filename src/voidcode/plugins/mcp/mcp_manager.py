"""MCP lifecycle manager: handles server initialization, discovery, and tool dispatch."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TypeVar

import httpx2
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import Implementation, PaginatedRequestParams, Tool
from referencing import Registry
from referencing.exceptions import Unresolvable

from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.provider import ProviderAbortSignal

from .types import (
    MCP_CLIENT_NAME,
    MCP_CLIENT_VERSION,
    REMOTE_HTTP_TRANSPORT,
    STDIO_TRANSPORT,
    McpCachedToolSurface,
    McpCallError,
    McpCallStage,
    McpConfig,
    McpConfigState,
    McpManager,
    McpManagerState,
    McpServerConfig,
    McpServerRuntimeState,
    McpServerStatus,
    McpToolCallResult,
    McpToolDescriptor,
    McpToolSafety,
)

DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_DISCOVERY_PAGES = 100
_MAX_DISCOVERY_TOOLS = 10_000
_SERVER_TEARDOWN_TIMEOUT_SECONDS = 10.0
_MCP_ABORT_POLL_SECONDS = 0.05
_MCP_REQUEST_TIMEOUT_CODES = frozenset({-32001, 408})
logger = logging.getLogger(__name__)
_RECOVERABLE_MCP_CALL_ERROR_CODES = frozenset(
    {
        -32602,  # JSON-RPC invalid params
        -32601,  # JSON-RPC method not found
        -32600,  # JSON-RPC invalid request
    }
)
#: The SDK's CONNECTION_CLOSED JSON-RPC error code (mcp_types.CONNECTION_CLOSED).
#: The streamable-http transport resolves an in-flight request with this error
#: when the underlying connection dies (e.g. the connect POST failed), which is
#: how an unreachable endpoint surfaces through ``ClientSession.initialize``.
_MCP_CONNECTION_CLOSED_CODE = -32000

_T = TypeVar("_T")


def _validate_input_schema(raw_schema: object) -> dict[str, object]:
    if not isinstance(raw_schema, dict):
        raise ValueError("inputSchema must be a JSON object")
    schema = dict(raw_schema)
    schema_type = schema.get("type")
    if schema_type is not None and schema_type != "object":
        raise ValueError("inputSchema type must be 'object'")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid MCP inputSchema: {exc.message}") from exc
    return schema


def _read_write_streams(transport_streams: tuple[object, ...]) -> tuple[Any, Any]:
    if len(transport_streams) < 2:
        raise ValueError("MCP transport context must provide read and write streams")
    return transport_streams[0], transport_streams[1]


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


def _validate_call_arguments_against_schema(
    *,
    tool_name: str,
    arguments: dict[str, object],
    input_schema: dict[str, object],
) -> None:
    try:
        # Schemas may use local definitions, but validation must not fetch remote references.
        Draft202012Validator(input_schema, registry=Registry()).validate(arguments)
    except Unresolvable as exc:
        raise ValueError(f"MCP[{tool_name}]: unresolved schema reference: {exc.ref}") from exc
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "arguments"
        raise ValueError(f"MCP[{tool_name}]: {path}: {exc.message}") from exc


# =============================================================================
# Disabled MCP Manager
# =============================================================================


class DisabledMcpManager:
    def __init__(self, config: McpConfig | None = None) -> None:
        self._configuration = McpConfigState.from_config(config)

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        return McpManagerState(configuration=self._configuration)

    def list_tools(
        self,
        *,
        workspace: Path,
        server_name: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]:
        _ = workspace, server_name
        raise ValueError("MCP support is disabled")

    def cached_surface(
        self,
        *,
        workspace: Path,
    ) -> McpCachedToolSurface:
        _ = workspace
        return McpCachedToolSurface()

    def descriptor_for(
        self,
        *,
        server_name: str,
        tool_name: str,
    ) -> McpToolDescriptor | None:
        _ = server_name, tool_name
        return None

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> McpToolCallResult:
        _ = server_name, tool_name, arguments, workspace, abort_signal
        raise McpCallError(
            "MCP support is disabled",
            boundary="pre_send",
            request_may_have_started=False,
        )

    def shutdown(self) -> None:
        return None


# =============================================================================
# Runtime MCP Server (SDK session lifecycle)
# =============================================================================


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


class _CallAttempt:
    """Thread-safe marker for whether a tools/call may have reached the server."""

    def __init__(self) -> None:
        self._started = threading.Event()

    @property
    def request_may_have_started(self) -> bool:
        return self._started.is_set()

    def mark_request_started(self) -> None:
        self._started.set()


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


@dataclass(frozen=True, slots=True)
class _ShutdownSnapshot:
    """The passive surfaces frozen at shutdown; reads keep working afterwards."""

    state: McpManagerState
    surface: McpCachedToolSurface
    descriptors: dict[str, dict[str, McpToolDescriptor]]


# =============================================================================
# Managed MCP Manager
# =============================================================================


class ManagedMcpManager:
    """Plugin-owned MCP lifecycle management backed by the official SDK."""

    def __init__(
        self,
        config: McpConfig,
    ) -> None:
        self._configuration = McpConfigState.from_config(config)
        self._running_servers: dict[str, _RunningMcpServer] = {}
        self._server_states = {name: McpServerRuntimeState() for name in self._configuration.servers}
        self._request_timeout_seconds = config.request_timeout_seconds or DEFAULT_MCP_REQUEST_TIMEOUT_SECONDS
        self._tool_descriptors_by_server: dict[str, dict[str, McpToolDescriptor]] = {}
        self._discovered_tools: dict[str, tuple[McpToolDescriptor, ...]] = {}
        self._startup_lock = asyncio.Lock()
        self._shutdown_called = False
        self._shutdown_snapshot: _ShutdownSnapshot | None = None
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._serve_loop, name="mcp-loop", daemon=True)
        self._loop_thread.start()

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

    @property
    def configuration(self) -> McpConfigState:
        return self._configuration

    def current_state(self) -> McpManagerState:
        return self._passive(self._current_state_owned, lambda snapshot: snapshot.state)

    async def _current_state_owned(self) -> McpManagerState:
        return McpManagerState(
            mode="managed",
            configuration=self._configuration,
            servers=dict(self._server_states),
        )

    def list_tools(
        self,
        *,
        workspace: Path,
        server_name: str | None = None,
    ) -> tuple[McpToolDescriptor, ...]:
        """Discover tools, connecting to configured servers.

        ``server_name`` narrows discovery to one server so an on-demand MCP
        tool call only pays for the server it actually needs.
        """
        return self._submit(self._list_tools_owned(workspace=workspace, server_name=server_name))

    async def _list_tools_owned(
        self,
        *,
        workspace: Path,
        server_name: str | None,
    ) -> tuple[McpToolDescriptor, ...]:
        server_names = (server_name,) if server_name is not None else tuple(self._configuration.servers)
        tools: list[McpToolDescriptor] = []
        for name in server_names:
            tools.extend(await self._list_tools_for_server_owned(server_name=name, workspace=workspace))
        return tuple(tools)

    def cached_surface(
        self,
        *,
        workspace: Path,
    ) -> McpCachedToolSurface:
        """Read discovered tools without connecting; absent servers remain unknown."""
        return self._passive(lambda: self._cached_surface_owned(workspace=workspace), lambda snapshot: snapshot.surface)

    async def _cached_surface_owned(self, *, workspace: Path) -> McpCachedToolSurface:
        _ = workspace
        return McpCachedToolSurface(servers=dict(self._discovered_tools))

    def descriptor_for(
        self,
        *,
        server_name: str,
        tool_name: str,
    ) -> McpToolDescriptor | None:
        """Descriptor discovered for one remote tool, or ``None`` when unknown.

        Passive: it answers only for a surface this manager already discovered
        (``call_tool`` discovers before it calls), never connecting on its own.
        """
        return self._passive(
            lambda: self._descriptor_for_owned(server_name=server_name, tool_name=tool_name),
            lambda snapshot: snapshot.descriptors.get(server_name, {}).get(tool_name),
        )

    async def _descriptor_for_owned(self, *, server_name: str, tool_name: str) -> McpToolDescriptor | None:
        return self._tool_descriptors_by_server.get(server_name, {}).get(tool_name)

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> McpToolCallResult:
        if self._configuration.servers.get(server_name) is None:
            raise McpCallError(
                f"MCP[{server_name}]: server not found in configuration",
                boundary="pre_send",
                request_may_have_started=False,
            )
        if abort_signal is not None and abort_signal.cancelled:
            raise McpCallError(
                "MCP call was cancelled before the request was sent",
                boundary="pre_send",
                request_may_have_started=False,
                cancelled=True,
            )
        attempt = _CallAttempt()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._call_tool_owned(
                    server_name=server_name,
                    tool_name=tool_name,
                    arguments=arguments,
                    workspace=workspace,
                    attempt=attempt,
                ),
                self._loop,
            )
        except Exception as exc:
            raise McpCallError(
                f"MCP[{server_name}/{tool_name}]: call could not be scheduled: {exc}",
                boundary="pre_send",
                request_may_have_started=False,
            ) from exc
        try:
            return self._await_call_future(future, abort_signal=abort_signal, attempt=attempt)
        except FutureCancelledError as exc:
            raise McpCallError(
                "MCP call was cancelled",
                boundary="cancelled",
                request_may_have_started=attempt.request_may_have_started,
                cancelled=True,
            ) from exc
        except McpCallError:
            raise
        except TimeoutError as exc:
            started = attempt.request_may_have_started
            raise McpCallError(
                str(exc) or "MCP call timed out",
                boundary="in_flight" if started else "pre_send",
                request_may_have_started=started,
                timed_out=True,
            ) from exc
        except ValueError as exc:
            raise McpCallError(str(exc), boundary="pre_send", request_may_have_started=False) from exc

    @staticmethod
    def _await_call_future(
        future: Future[McpToolCallResult],
        *,
        abort_signal: ProviderAbortSignal | None,
        attempt: _CallAttempt,
    ) -> McpToolCallResult:
        if abort_signal is None:
            return future.result()
        while True:
            try:
                return future.result(timeout=_MCP_ABORT_POLL_SECONDS)
            except FutureTimeoutError:
                if future.done():
                    raise
                if not abort_signal.cancelled:
                    continue
                future.cancel()
                started = attempt.request_may_have_started
                raise McpCallError(
                    "MCP call was cancelled after the request may have reached the server; external effects are unknown"
                    if started
                    else "MCP call was cancelled before the request was sent",
                    boundary="cancelled",
                    request_may_have_started=started,
                    cancelled=True,
                ) from None

    async def _call_tool_owned(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
        attempt: _CallAttempt,
    ) -> McpToolCallResult:
        running = await self._ensure_running_owned(server_name=server_name, workspace=workspace)
        descriptor = self._tool_descriptors_by_server.get(server_name, {}).get(tool_name)
        if descriptor is None:
            _ = await self._list_tools_for_server_owned(server_name=server_name, workspace=workspace)
            descriptor = self._tool_descriptors_by_server.get(server_name, {}).get(tool_name)

        if descriptor is None:
            raise ValueError(f"MCP[{server_name}/{tool_name}]: tool not discovered on this server")

        if not descriptor.enabled:
            disabled_reason = descriptor.disabled_reason
            message = f"MCP[{server_name}/{tool_name}] is disabled"
            if disabled_reason:
                message = f"{message}: {disabled_reason}"
            raise ValueError(message)

        _validate_call_arguments_against_schema(
            tool_name=f"{server_name}/{tool_name}",
            arguments=arguments,
            input_schema=descriptor.input_schema,
        )

        def _call_remote():
            attempt.mark_request_started()
            return running.session.call_tool(
                tool_name,
                dict(arguments),
                read_timeout_seconds=self._request_timeout_seconds,
            )

        result = await self._call_sdk_owned(
            running,
            stage=McpCallStage.CALL,
            method="tools/call",
            operation=_call_remote,
            attempt=attempt,
        )
        call_result = result
        try:
            raw_structured_content = call_result.structured_content
            if raw_structured_content is None:
                structured_content = None
            elif isinstance(raw_structured_content, Mapping):
                structured_content = json_wire_object(own_json_object(raw_structured_content))
            else:
                raise ValueError("MCP structured content must be a JSON object")
            content = [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in call_result.content]
        except (TypeError, ValueError) as exc:
            raise McpCallError(
                f"MCP[{server_name}/{tool_name}]: invalid result after dispatch: {exc}",
                boundary="in_flight",
                request_may_have_started=True,
            ) from exc
        return McpToolCallResult(
            content=content,
            structured_content=structured_content,
            is_error=call_result.is_error,
        )

    def shutdown(self) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        try:
            snapshot = asyncio.run_coroutine_threadsafe(self._shutdown_owned(), self._loop).result(timeout=30.0)
        except Exception as exc:
            logger.warning("MCP shutdown did not complete: %s: %s", type(exc).__name__, exc)
        else:
            self._shutdown_snapshot = snapshot
        finally:
            # Passive reads after close must never wait on a loop that is about to stop.
            if self._shutdown_snapshot is None:
                self._shutdown_snapshot = self._fallback_shutdown_snapshot()
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=30.0)
            if not self._loop_thread.is_alive() and not self._loop.is_closed():
                self._loop.close()

    async def _shutdown_owned(self) -> _ShutdownSnapshot:
        keys = tuple(self._running_servers)
        results = await asyncio.gather(
            *(self._stop_running_server_owned(key, timeout=_SERVER_TEARDOWN_TIMEOUT_SECONDS) for key in keys),
            return_exceptions=True,
        )
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BaseException):
                message = f"MCP[{key}]: teardown failed: {type(result).__name__}: {result}"
                logger.warning(message)
                self._set_server_state_owned(key=key, status="failed", error=message)
        return self._snapshot_owned()

    def _snapshot_owned(self) -> _ShutdownSnapshot:
        return _ShutdownSnapshot(
            state=McpManagerState(mode="managed", configuration=self._configuration, servers=dict(self._server_states)),
            surface=McpCachedToolSurface(servers=dict(self._discovered_tools)),
            descriptors={server: dict(descriptors) for server, descriptors in self._tool_descriptors_by_server.items()},
        )

    def _fallback_shutdown_snapshot(self) -> _ShutdownSnapshot:
        for key in tuple(self._running_servers):
            self._set_server_state_owned(
                key=key,
                status="failed",
                error="MCP shutdown did not complete; server teardown state is unknown",
            )
        return self._snapshot_owned()

    async def _ensure_running_owned(
        self,
        *,
        server_name: str,
        workspace: Path,
    ) -> _RunningMcpServer:
        """Ensure MCP server is running, start if needed."""
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            message = f"MCP[{server_name}]: server not found in configuration. Available servers: {list(self._configuration.servers.keys())}"
            self._set_server_state_owned(key=server_name, status="failed", error=message)
            raise ValueError(message)

        running = self._running_servers.get(server_name)
        if running is not None:
            return running

        async with self._startup_lock:
            running = self._running_servers.get(server_name)
            if running is not None:
                return running

            transport = server_config.transport
            key = server_name
            workspace_root = workspace.resolve()
            stderr_log: IO[str] | None = None
            transport_context: _OwnedAsyncContext | None = None
            session_context: _OwnedAsyncContext | None = None

            try:
                transport_context, session_context, session, stderr_log = await self._open_session_owned(
                    server_name=server_name,
                    server_config=server_config,
                    workspace_root=workspace_root,
                )
                self._set_server_state_owned(key=key, status="starting")
                await session.initialize()
                self._set_server_state_owned(key=key, status="running")
            except FileNotFoundError as exc:
                await self._close_partial_server_owned(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if transport == STDIO_TRANSPORT:
                    message = f"MCP[{server_name}]: failed to start server - cmd not found (command not found): {server_config.command[0]}"
                    self._set_server_state_owned(key=key, status="failed", error=message)
                else:
                    message = self._report_remote_connect_failure_owned(
                        key=key,
                        server_name=server_name,
                    )
                raise ValueError(message) from exc
            except BaseExceptionGroup as exc:
                # SDK transport failures may be grouped. Control exceptions must
                # retain their identity instead of becoming connection errors.
                await self._close_partial_server_owned(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if not isinstance(exc, Exception):
                    raise
                leaf = _flatten_exception_group(exc)
                if transport == REMOTE_HTTP_TRANSPORT and _is_connection_error(leaf):
                    message = self._report_remote_connect_failure_owned(
                        key=key,
                        server_name=server_name,
                    )
                else:
                    message = self._message_for_exception(
                        leaf,
                        fallback=f"MCP[{server_name}]: failed to initialize server",
                    )
                    self._set_server_state_owned(key=key, status="failed", error=message)
                raise ValueError(message) from exc
            except Exception as exc:
                await self._close_partial_server_owned(
                    session_context=session_context,
                    transport_context=transport_context,
                    stderr_log=stderr_log,
                )
                if transport == REMOTE_HTTP_TRANSPORT and _is_connection_error(exc):
                    message = self._report_remote_connect_failure_owned(
                        key=key,
                        server_name=server_name,
                    )
                else:
                    message = self._message_for_exception(
                        exc,
                        fallback=f"MCP[{server_name}]: failed to initialize server",
                    )
                    self._set_server_state_owned(key=key, status="failed", error=message)
                raise ValueError(message) from exc

            running = _RunningMcpServer(
                server_name=server_name,
                transport_context=transport_context,
                session_context=session_context,
                session=session,
                stderr_log=stderr_log,
            )
            self._running_servers[key] = running
            return running

    async def _open_session_owned(
        self,
        *,
        server_name: str,
        server_config: McpServerConfig,
        workspace_root: Path,
    ) -> tuple[_OwnedAsyncContext, _OwnedAsyncContext, ClientSession, IO[str] | None]:
        """Enter the transport and session contexts for one server start."""
        _ = server_name
        stderr_log: IO[str] | None = None
        transport_context: _OwnedAsyncContext | None = None
        session_context: _OwnedAsyncContext | None = None
        try:
            if server_config.transport == REMOTE_HTTP_TRANSPORT:
                if server_config.url is None:
                    raise ValueError(f"remote-http MCP server {server_name!r} requires a url")
                transport_context = _OwnedAsyncContext(streamable_http_client(server_config.url))
                read_stream, write_stream = _read_write_streams(await transport_context.enter())
            else:
                stderr_log = open(
                    os.devnull,
                    "w",
                    encoding="utf-8",
                )
                params = StdioServerParameters(
                    command=server_config.command[0],
                    args=list(server_config.command[1:]),
                    env={**os.environ, **server_config.env},
                    cwd=workspace_root,
                )
                transport_context = _OwnedAsyncContext(stdio_client(params, errlog=stderr_log))
                read_stream, write_stream = _read_write_streams(await transport_context.enter())
            session_context = _OwnedAsyncContext(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._request_timeout_seconds,
                    client_info=Implementation(
                        name=MCP_CLIENT_NAME,
                        version=MCP_CLIENT_VERSION,
                    ),
                )
            )
            session = await session_context.enter()
        except BaseException:
            if session_context is not None:
                with suppress(BaseException):
                    await session_context.exit()
            if transport_context is not None:
                with suppress(BaseException):
                    await transport_context.exit()
            if stderr_log is not None:
                stderr_log.close()
            raise
        return transport_context, session_context, session, stderr_log

    async def _call_sdk_owned[T](
        self,
        running: _RunningMcpServer,
        *,
        stage: McpCallStage,
        method: str,
        operation: Callable[[], Coroutine[Any, Any, T]],
        suppress_failure_event_when: Callable[[Exception], bool] | None = None,
        attempt: _CallAttempt | None = None,
    ) -> T:
        async with running.io_lock:
            if self._running_servers.get(running.server_name) is not running:
                raise ValueError(f"MCP[{running.server_name}]: connection was replaced before {method}; the request was not sent")
            try:
                return await operation()
            except Exception as exc:
                message = self._message_for_exception(
                    exc,
                    fallback=f"MCP[{running.server_name}]: {method} failed",
                )
                if not self._is_recoverable_call_error(exc, stage=stage):
                    await self._stop_running_server_owned(running.server_name, expected=running)
                    if suppress_failure_event_when is None or not suppress_failure_event_when(exc):
                        if running.server_name not in self._running_servers:
                            self._set_server_state_owned(key=running.server_name, status="failed", error=message)
                if stage is McpCallStage.CALL:
                    started = attempt is not None and attempt.request_may_have_started
                    raise McpCallError(
                        message,
                        boundary="in_flight" if started else "pre_send",
                        request_may_have_started=started,
                        timed_out=self._is_timeout_error(exc),
                    ) from exc
                raise ValueError(message) from exc

    def _is_recoverable_call_error(self, exc: Exception, *, stage: McpCallStage) -> bool:
        if stage is not McpCallStage.CALL or not isinstance(exc, MCPError) or self._is_timeout_error(exc):
            return False
        return exc.code in _RECOVERABLE_MCP_CALL_ERROR_CODES

    def _descriptor_from_sdk_tool(self, *, server_name: str, tool: Tool) -> McpToolDescriptor:
        annotations = tool.annotations
        safety = (
            McpToolSafety.from_hints(
                read_only_hint=annotations.read_only_hint,
                destructive_hint=annotations.destructive_hint,
                idempotent_hint=annotations.idempotent_hint,
                open_world_hint=annotations.open_world_hint,
            )
            if annotations is not None
            else McpToolSafety()
        )
        try:
            input_schema = _validate_input_schema(tool.input_schema)
            return McpToolDescriptor(
                server_name=server_name,
                tool_name=tool.name,
                description=tool.description or "",
                input_schema=input_schema,
                safety=safety,
                enabled=True,
            )
        except ValueError as exc:
            return McpToolDescriptor(
                server_name=server_name,
                tool_name=tool.name,
                description=tool.description or "",
                input_schema={"type": "object", "properties": {}},
                safety=safety,
                enabled=False,
                disabled_reason=str(exc),
            )

    async def _list_tools_for_server_owned(
        self,
        *,
        server_name: str,
        workspace: Path,
    ) -> tuple[McpToolDescriptor, ...]:
        server_config = self._configuration.servers.get(server_name)
        if server_config is None:
            message = f"MCP[{server_name}]: server not found in configuration. Available servers: {list(self._configuration.servers.keys())}"
            self._set_server_state_owned(key=server_name, status="failed", error=message)
            raise ValueError(message)
        running = await self._ensure_running_owned(server_name=server_name, workspace=workspace)
        try:
            sdk_tools = await self._list_tools_pages_owned(
                running,
                server_name=server_name,
                suppress_failure_event_when=_is_discovery_connection_closed,
            )
        except ValueError as exc:
            underlying = exc.__cause__ if isinstance(exc.__cause__, BaseException) else exc
            if not _is_discovery_connection_closed(underlying):
                raise
            # The streamable-http transport resolved an in-flight tools/list
            # POST with CONNECTION_CLOSED (-32000): the upstream connection
            # dropped mid-request (e.g. a Cloudflare-flapped remote-http
            # endpoint). That is transient, so retry the whole paginated
            # discovery once with a fresh connection before failing. The
            # failure event was suppressed on the probe attempt and is only
            # recorded if the retry also fails.
            await self._stop_running_server_owned(server_name, expected=running)
            running = await self._ensure_running_owned(server_name=server_name, workspace=workspace)
            sdk_tools = await self._list_tools_pages_owned(running, server_name=server_name)
        server_descriptors: dict[str, McpToolDescriptor] = {}
        descriptors: list[McpToolDescriptor] = []
        for tool in sdk_tools:
            descriptor = self._descriptor_from_sdk_tool(server_name=server_name, tool=tool)
            server_descriptors[descriptor.tool_name] = descriptor
            descriptors.append(descriptor)
        discovered = tuple(descriptors)
        if self._running_servers.get(server_name) is running:
            self._tool_descriptors_by_server[server_name] = server_descriptors
            self._discovered_tools[server_name] = discovered
        return discovered

    async def _list_tools_pages_owned(
        self,
        running: _RunningMcpServer,
        *,
        server_name: str,
        suppress_failure_event_when: Callable[[Exception], bool] | None = None,
    ) -> list[Tool]:
        """Read every ``tools/list`` page, bounded by page and tool caps."""
        tools: list[Tool] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_DISCOVERY_PAGES):
            params = None if cursor is None else PaginatedRequestParams(cursor=cursor)
            result = await self._call_sdk_owned(
                running,
                stage=McpCallStage.DISCOVERY,
                method="tools/list",
                operation=lambda params=params: running.session.list_tools(params=params),
                suppress_failure_event_when=suppress_failure_event_when,
            )
            tools.extend(result.tools)
            if len(tools) > _MAX_DISCOVERY_TOOLS:
                raise ValueError(f"MCP[{server_name}]: tools/list returned more than {_MAX_DISCOVERY_TOOLS} tools")
            next_cursor = result.next_cursor
            if next_cursor is None:
                return tools
            if next_cursor in seen_cursors:
                raise ValueError(f"MCP[{server_name}]: tools/list returned a repeated cursor; discovery did not complete")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise ValueError(f"MCP[{server_name}]: tools/list exceeded {_MAX_DISCOVERY_PAGES} pages; discovery did not complete")

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

    def _set_server_state_owned(self, *, key: str, status: McpServerStatus, error: str | None = None) -> None:
        self._server_states[key] = McpServerRuntimeState(status=status, error=error)

    def _report_remote_connect_failure_owned(
        self,
        *,
        key: str,
        server_name: str,
    ) -> str:
        """Record the startup failure for an unreachable remote-http endpoint.

        Returns the clean user-facing message raised as a ``ValueError``.
        """
        message = f"MCP[{server_name}]: failed to connect to remote server"
        self._set_server_state_owned(key=key, status="failed", error=message)
        return message

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


# =============================================================================
# Factory
# =============================================================================


def build_mcp_manager(
    config: McpConfig | None,
) -> McpManager:
    """Build an MCP manager based on configuration.

    Anything that cannot reach a server (no config, disabled, no servers) gets
    the disabled manager, so the tool reports "disabled" instead of pretending
    a connection attempt happened.
    """
    if config is None:
        return DisabledMcpManager(None)
    state = McpConfigState.from_config(config)
    if state.configured_enabled is not True or not state.servers:
        return DisabledMcpManager(config)
    return ManagedMcpManager(
        config,
    )
