"""MCP shutdown and the frozen passive snapshot that survives close.

This module is composed into the `ManagedMcpManager` facade. It owns the
shutdown sequence and snapshot shape; server connection teardown itself stays
with the runtime connection lifecycle.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass

from ._runtime import _ManagerState, logger
from .types import (
    McpCachedToolSurface,
    McpManagerState,
    McpToolDescriptor,
)


@dataclass(frozen=True, slots=True)
class _ShutdownSnapshot:
    """The passive surfaces frozen at shutdown; reads keep working afterwards."""

    state: McpManagerState
    surface: McpCachedToolSurface
    descriptors: dict[str, dict[str, McpToolDescriptor]]


class _ShutdownMixin(_ManagerState):
    """Own manager shutdown and the post-close passive snapshot."""

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
            *(self._stop_running_server_owned(key, timeout=self._teardown_timeout_seconds()) for key in keys),
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
