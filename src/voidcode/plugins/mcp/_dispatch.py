"""MCP tool dispatch: scheduling, cancellation, call boundaries, and retry classes.

This module is composed into the `ManagedMcpManager` facade. Discovery and
session startup stay in `_session`; this module only turns a validated tool call
into an SDK dispatch and maps failures to typed call errors.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

from mcp.shared.exceptions import MCPError

from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.provider import ProviderAbortSignal

from ._runtime import _CallAttempt, _ManagerState, _RunningMcpServer
from ._session import _validate_call_arguments_against_schema
from .types import McpCallError, McpCallStage, McpToolCallResult

_MCP_ABORT_POLL_SECONDS = 0.05
_RECOVERABLE_MCP_CALL_ERROR_CODES = frozenset(
    {
        -32602,  # JSON-RPC invalid params
        -32601,  # JSON-RPC method not found
        -32600,  # JSON-RPC invalid request
    }
)


class _DispatchMixin(_ManagerState):
    """Dispatch one validated tool call and classify its failure boundary."""

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
