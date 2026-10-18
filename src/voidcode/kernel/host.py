"""HostClient managing isolated plugin host processes and invocation lifecycles."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from voidcode.sdk.codec import (
    decode_provider_error,
    decode_provider_result,
    decode_tool_definition,
    decode_tool_result,
    encode_context,
    encode_provider_request,
    encode_tool_call,
)
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import HostPlugin
from voidcode.sdk.provider import ProviderAbortSignal, ProviderExecutionError, ProviderTurnRequest, ProviderTurnResult
from voidcode.sdk.tools import ToolCall, ToolDefinition, ToolResult

__all__ = [
    "HostClient",
    "HostTimeout",
    "HostUnavailable",
    "InvocationCancelled",
]

MAX_FRAME_SIZE: int = 1024 * 1024  # 1 MiB


class HostUnavailable(RuntimeError):
    """Raised when the plugin host is unavailable or transport failed."""

    def __init__(self, message: str = "Plugin host is unavailable", *, execution_stopped: bool = False) -> None:
        super().__init__(message)
        self.execution_stopped = execution_stopped


class HostTimeout(TimeoutError):
    """Raised when a plugin invocation times out."""

    def __init__(self, message: str = "Plugin host invocation timed out", *, execution_stopped: bool = False) -> None:
        super().__init__(message)
        self.execution_stopped = execution_stopped


class InvocationCancelled(RuntimeError):
    """Raised when an invocation is cancelled."""

    def __init__(self, message: str = "Invocation was cancelled", *, execution_stopped: bool = False) -> None:
        super().__init__(message)
        self.execution_stopped = execution_stopped


class HostClient:
    """Client connection to an isolated plugin host subprocess."""

    def __init__(
        self,
        plugins: tuple[HostPlugin, ...],
        *,
        workspace: Path,
        revision_id: str,
        lease_fd: int | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace directory does not exist: {self.workspace}")

        if not revision_id:
            raise ValueError("revision_id must be a non-empty string")
        self.revision_id = revision_id

        # Client admission checks
        seen_pids: set[str] = set()
        seen_tools: set[str] = set()
        seen_providers: set[str] = set()
        seen_modules: set[str] = set()

        for plugin in plugins:
            pid = plugin.manifest.plugin_id
            if pid in seen_pids:
                raise ValueError(f"Duplicate plugin id: {pid}")
            seen_pids.add(pid)

            # Check entrypoint and module collision using top-level package name
            if ":" not in plugin.manifest.entrypoint:
                raise ValueError(f"Invalid entrypoint format: {plugin.manifest.entrypoint}")
            mod_full = plugin.manifest.entrypoint.split(":", 1)[0]
            top_mod = mod_full.split(".", 1)[0]
            if top_mod in seen_modules:
                raise ValueError(f"Top-level module conflict across plugins: {top_mod}")
            seen_modules.add(top_mod)

            # Check grants: plugin capabilities must cover all its tools' required capabilities
            for tool_decl in plugin.manifest.tools:
                req_caps = frozenset(tool_decl.capabilities)
                if not req_caps.issubset(plugin.capabilities):
                    missing = req_caps - plugin.capabilities
                    raise ValueError(f"Plugin {pid} tool {tool_decl.name} requires capabilities {set(missing)} not granted to plugin")
                if tool_decl.name in seen_tools:
                    raise ValueError(f"Duplicate tool name declared across plugins: {tool_decl.name}")
                seen_tools.add(tool_decl.name)

            for prov_decl in plugin.manifest.providers:
                if prov_decl.name in seen_providers:
                    raise ValueError(f"Duplicate provider name declared across plugins: {prov_decl.name}")
                seen_providers.add(prov_decl.name)

        self._plugins = plugins
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._maps_lock = threading.Lock()
        self._pending: dict[str, Future[dict[str, Any]]] = {}
        self._counter: int = 0
        self._closed: bool = False

        # Spawn plugin host process in a new process session (process group leader)
        pass_fds = () if lease_fd is None else (lease_fd,)
        cmd = [sys.executable, "-m", "voidcode.plugin_host"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=sys.stderr,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        except Exception as exc:
            raise HostUnavailable(f"Failed to spawn plugin host process: {exc}", execution_stopped=True) from exc

        self._pid = self._proc.pid

        # Start reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"host-client-reader-{self._pid}",
        )
        self._reader_thread.start()

        # Perform init handshake
        try:
            self._init_host()
        except Exception:
            self._kill_and_reap()
            raise

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def tools(self) -> tuple[ToolDefinition, ...]:
        return self._tools

    @property
    def providers(self) -> tuple[str, ...]:
        return self._providers

    def owner(self, tool_name: str) -> str:
        if tool_name not in self._tool_owners:
            raise KeyError(f"Tool {tool_name} not found in catalog")
        return self._tool_owners[tool_name]

    def capabilities(self, tool_name: str) -> frozenset[str]:
        if tool_name not in self._tool_capabilities:
            raise KeyError(f"Tool {tool_name} not found in catalog")
        return self._tool_capabilities[tool_name]

    def _next_id(self, prefix: str) -> str:
        with self._maps_lock:
            self._counter += 1
            return f"{prefix}-{self._counter}"

    def _kill_group(self, sig: int = signal.SIGKILL) -> None:
        """Signal the entire process group spawned for the host."""
        if self._proc is None:
            return
        pid = self._proc.pid
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, sig)
        except ProcessLookupError, PermissionError:
            pass
        try:
            self._proc.send_signal(sig)
        except ProcessLookupError, PermissionError:
            pass

    def _send_request(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, allow_nan=False).encode("utf-8") + b"\n"
        if len(data) > MAX_FRAME_SIZE:
            raise ValueError(f"Request exceeds 1MiB frame limit ({len(data)} bytes)")
        with self._write_lock:
            if self._proc.stdin is None or self._proc.stdin.closed:
                raise HostUnavailable("Host stdin is closed", execution_stopped=False)
            self._proc.stdin.write(data)
            self._proc.stdin.flush()

    def _init_host(self) -> None:
        req_id = "init"
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future

        plugins_spec = [
            {
                "root": str(p.root.resolve()),
                "manifest": p.manifest.as_payload(),
                "config": json_wire_object(p.config),
                "capabilities": sorted(list(p.capabilities)),
            }
            for p in self._plugins
        ]

        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "init",
                    "params": {
                        "workspace": str(self.workspace),
                        "revision_id": self.revision_id,
                        "plugins": plugins_spec,
                    },
                }
            )
            response = future.result(timeout=30.0)
        except Exception as exc:
            self._kill_and_reap()
            is_stopped = self._proc.poll() is not None if self._proc else True
            raise HostUnavailable(f"Host initialization failed: {exc}", execution_stopped=is_stopped) from exc
        finally:
            with self._maps_lock:
                self._pending.pop(req_id, None)

        if not response.get("ok", False):
            err = response.get("error", {})
            self._kill_and_reap()
            is_stopped = self._proc.poll() is not None if self._proc else True
            raise HostUnavailable(f"Host initialization rejected: {err.get('message', 'unknown error')}", execution_stopped=is_stopped)

        result = response.get("result", {})
        self._tools = tuple(decode_tool_definition(t) for t in result.get("tools", ()))
        self._tool_map = {t.name: t for t in self._tools}
        self._providers = tuple(str(p) for p in result.get("providers", ()))
        self._tool_owners = dict(result.get("tool_owners", {}))
        self._tool_capabilities = {k: frozenset(v) for k, v in result.get("tool_capabilities", {}).items()}

    def _reader_loop(self) -> None:
        try:
            assert self._proc.stdout is not None
            while True:
                raw_line = self._proc.stdout.readline(MAX_FRAME_SIZE + 1)
                if not raw_line:
                    break
                if len(raw_line) > MAX_FRAME_SIZE or not raw_line.endswith(b"\n"):
                    # Frame too large or missing newline -> fail closed
                    break

                line = raw_line.strip()
                if not line:
                    continue

                try:
                    message = json.loads(line.decode("utf-8"))
                except Exception:
                    break

                if not isinstance(message, dict):
                    break

                protocol_version = message.get("protocol_version")
                req_id = message.get("id")
                ok = message.get("ok")
                if type(protocol_version) is not int or protocol_version != 1:
                    break
                if not isinstance(req_id, str) or not req_id:
                    break
                if type(ok) is not bool:
                    break

                allowed_keys = {"protocol_version", "id", "ok", "result"} if ok else {"protocol_version", "id", "ok", "error"}
                if set(message.keys()) != allowed_keys:
                    break

                if not ok:
                    err = message.get("error")
                    if not isinstance(err, dict) or "kind" not in err or "message" not in err:
                        break

                with self._maps_lock:
                    fut = self._pending.get(req_id)
                if fut is None or fut.done():
                    break
                fut.set_result(message)
        except Exception:
            pass
        finally:
            with self._lifecycle_lock:
                if not self._closed:
                    self._closed = True
                    if self._proc is not None and self._proc.poll() is None:
                        self._kill_group(signal.SIGTERM)
                        try:
                            self._proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            self._kill_group(signal.SIGKILL)
                            try:
                                self._proc.wait(timeout=2.0)
                            except subprocess.TimeoutExpired:
                                pass
                    is_stopped = self._proc.poll() is not None if self._proc else True
                    self._reap_pending("Host process closed transport or sent malformed frame (EOF)", execution_stopped=is_stopped)

    def _reap_pending(self, reason: str, *, execution_stopped: bool) -> None:
        with self._maps_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(HostUnavailable(reason, execution_stopped=execution_stopped))

    def _kill_and_reap(self) -> bool:
        with self._lifecycle_lock:
            self._closed = True
            if self._proc is not None and self._proc.poll() is None:
                self._kill_group(signal.SIGTERM)
                try:
                    self._proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self._kill_group(signal.SIGKILL)
                    try:
                        self._proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
            is_stopped = self._proc.poll() is not None if self._proc else True
            self._reap_pending("Host process killed and reaped", execution_stopped=is_stopped)
            return is_stopped

    def _attempt_cooperative_cancel(self, req_id: str) -> None:
        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": self._next_id("cancel"),
                    "method": "cancel",
                    "params": {"target_id": req_id},
                }
            )
        except Exception:
            pass

    def _wait_for_future(
        self,
        req_id: str,
        future: Future[dict[str, Any]],
        timeout_seconds: float,
        abort_signal: ProviderAbortSignal | None,
    ) -> dict[str, Any]:
        done_event = threading.Event()
        future.add_done_callback(lambda _: done_event.set())

        start_time = time.monotonic()
        cancelled_by_signal = False

        while True:
            if future.done():
                break
            if abort_signal is not None and abort_signal.cancelled:
                cancelled_by_signal = True
                break
            elapsed = time.monotonic() - start_time
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                break
            done_event.wait(timeout=min(remaining, 0.1))

        if abort_signal is not None and abort_signal.cancelled:
            cancelled_by_signal = True

        try:
            if future.done() and not cancelled_by_signal:
                return future.result()

            if cancelled_by_signal:
                self._attempt_cooperative_cancel(req_id)
                cancelled_cleanly = False
                try:
                    future.result(timeout=1.0)
                    cancelled_cleanly = True
                except TimeoutError:
                    pass
                except Exception:
                    pass

                if cancelled_cleanly:
                    raise InvocationCancelled("Invocation was cancelled", execution_stopped=True)
                stopped = self._kill_and_reap()
                raise InvocationCancelled("Invocation was cancelled; host process killed", execution_stopped=stopped)

            # Timeout occurred
            self._attempt_cooperative_cancel(req_id)
            stopped_cleanly = False
            try:
                future.result(timeout=0.5)
                stopped_cleanly = True
            except TimeoutError:
                pass
            except Exception:
                pass

            if stopped_cleanly:
                raise HostTimeout(f"Invocation timed out after {timeout_seconds}s", execution_stopped=True)
            stopped = self._kill_and_reap()
            raise HostTimeout(f"Invocation timed out after {timeout_seconds}s; host killed", execution_stopped=stopped)
        finally:
            with self._maps_lock:
                self._pending.pop(req_id, None)

    def invoke(self, call: ToolCall, *, context: ToolContext, timeout_seconds: float = 120) -> ToolResult:
        """Invoke a registered tool in the host process."""
        if self._closed or self._proc is None or self._proc.poll() is not None:
            is_stopped = self._proc.poll() is not None if self._proc else True
            raise HostUnavailable("Plugin host is closed or process terminated", execution_stopped=is_stopped)

        # Validate context workspace and revision against client
        if context.workspace.resolve() != self.workspace.resolve() or context.revision_id != self.revision_id:
            raise ValueError(
                f"Context mismatch: expected workspace {self.workspace} revision {self.revision_id}, "
                f"got {context.workspace} revision {context.revision_id}"
            )

        # Validate tool in catalog
        if call.tool_name not in self._tool_map:
            raise ValueError(f"Unknown tool: {call.tool_name}")

        # Validate capabilities grant
        tool_caps = self.capabilities(call.tool_name)
        if not tool_caps.issubset(context.capabilities):
            missing = tool_caps - context.capabilities
            raise PermissionError(f"Invocation context lacks required capability grants for {call.tool_name}: missing {set(missing)}")

        req_id = self._next_id("inv")
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future

        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "invoke",
                    "params": {
                        "call": encode_tool_call(call),
                        "context": encode_context(context),
                    },
                }
            )
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send invoke request: {exc}", execution_stopped=stopped) from exc

        response = self._wait_for_future(req_id, future, timeout_seconds, context.abort_signal)
        if not response.get("ok", False):
            err = response.get("error", {})
            raise RuntimeError(err.get("message", "Invocation failed") if isinstance(err, dict) else "Invocation failed")

        return decode_tool_result(response["result"])

    def propose_turn(
        self,
        request: ProviderTurnRequest,
        *,
        provider: str = "openai-chat",
        timeout_seconds: float = 120,
    ) -> ProviderTurnResult:
        """Request a model turn from a registered provider."""
        if self._closed or self._proc is None or self._proc.poll() is not None:
            is_stopped = self._proc.poll() is not None if self._proc else True
            raise HostUnavailable("Plugin host is closed or process terminated", execution_stopped=is_stopped)

        if provider not in self.providers:
            raise ProviderExecutionError(
                kind="not_configured",
                provider_name=provider,
                model_name=request.model_name or "unknown",
                message=f"Provider '{provider}' not configured",
            )

        req_id = self._next_id("turn")
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future

        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "propose_turn",
                    "params": {
                        "provider": provider,
                        "request": encode_provider_request(request),
                    },
                }
            )
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send propose_turn request: {exc}", execution_stopped=stopped) from exc

        response = self._wait_for_future(req_id, future, timeout_seconds, request.abort_signal)
        if not response.get("ok", False):
            err = response.get("error", {})
            if isinstance(err, dict) and "provider_error" in err:
                raise decode_provider_error(err["provider_error"])
            raise RuntimeError(err.get("message", "Provider turn failed") if isinstance(err, dict) else "Provider turn failed")

        return decode_provider_result(response["result"])

    def close(self) -> None:
        """Close connection to host and reap child process group cleanly."""
        with self._lifecycle_lock:
            if not self._closed:
                self._closed = True
                if self._proc is not None and self._proc.poll() is None:
                    try:
                        self._send_request(
                            {
                                "protocol_version": 1,
                                "id": "close",
                                "method": "close",
                                "params": {},
                            }
                        )
                        self._proc.wait(timeout=1.0)
                    except Exception:
                        pass
                    if self._proc.poll() is None:
                        self._kill_group(signal.SIGTERM)
                        try:
                            self._proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            self._kill_group(signal.SIGKILL)
                            try:
                                self._proc.wait(timeout=2.0)
                            except subprocess.TimeoutExpired:
                                pass
            is_stopped = self._proc.poll() is not None if self._proc else True
            self._reap_pending("HostClient closed", execution_stopped=is_stopped)

    def __enter__(self) -> HostClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
