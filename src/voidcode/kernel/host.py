"""HostClient managing isolated plugin host processes and invocation lifecycles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from voidcode.sdk.codec import (
    decode_command_descriptor,
    decode_command_result,
    decode_provider_error,
    decode_provider_result,
    decode_tool_definition,
    decode_tool_result,
    decode_view_descriptor,
    encode_context,
    encode_provider_request,
    encode_tool_call,
)
from voidcode.sdk.commands import CommandDescriptor, CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.plugin import HostPlugin
from voidcode.sdk.provider import ProviderAbortSignal, ProviderExecutionError, ProviderTurnRequest, ProviderTurnResult
from voidcode.sdk.services import PluginBudget, SessionUsageReport
from voidcode.sdk.tools import ToolCall, ToolDefinition, ToolEffect, ToolResult, is_read_tier
from voidcode.sdk.views import PluginViewDescriptor

from .artifacts import SHA256_PATTERN, ArtifactStore, normalize_media_type
from .storage.store import (
    EventDraft,
    SqliteStore,
    StagedCancellation,
    StagedContinuationDraft,
    StagedStateUpdate,
    canonical_json,
)

__all__ = [
    "CallbackChanges",
    "HostClient",
    "HostTimeout",
    "HostUnavailable",
    "InvocationCancelled",
    "ToolInvocationOutcome",
]
MAX_FRAME_SIZE: int = 1024 * 1024  # 1 MiB


def _require_int(value: object, field_name: str) -> int:
    """Protocol ingress: accept only genuine ints, never bool/float/str laundering."""
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an integer")
    return value


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


@dataclass
class LeaseContext:
    token: str
    owner: str
    session_id: str
    workspace: Path
    run_id: str
    revision_id: str
    invocation_id: str
    grants: frozenset[str]
    readonly: bool
    #: Authentic actor of the invocation that created this lease ("model", "user", "plugin").
    actor: str = "model"
    #: Hook leases are observers of a settled invocation: they can never start nested work.
    hook: bool = False
    invocation_depth: int = 0
    staged_state: dict[tuple[str, str], tuple[Mapping[str, object], int, int]] = field(default_factory=dict)
    staged_events: list[EventDraft] = field(default_factory=list)
    staged_continuations: list[StagedContinuationDraft] = field(default_factory=list)
    staged_cancellations: list[StagedCancellation] = field(default_factory=list)
    staged_budget: PluginBudget | None | Literal["clear"] = None
    revoked: bool = False
    op_counter: int = 0


@dataclass(frozen=True, slots=True)
class ToolInvocationOutcome:
    result: ToolResult
    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_budget: PluginBudget | None | Literal["clear"] = None


@dataclass(frozen=True, slots=True)
class CallbackChanges:
    staged_state: tuple[StagedStateUpdate, ...] = ()
    staged_events: tuple[EventDraft, ...] = ()
    staged_continuations: tuple[StagedContinuationDraft, ...] = ()
    staged_cancellations: tuple[StagedCancellation, ...] = ()
    staged_budgets: tuple[tuple[str, PluginBudget | Literal["clear"]], ...] = ()
    errors: tuple[dict[str, str], ...] = ()


#: One spill chunk must fit one 1 MiB RPC frame even after base64 encoding.
_ARTIFACT_CHUNK_BYTES = 256 * 1024


def _require_sha256(value: object) -> str:
    """One content address, validated before it can ever become a path."""
    if not isinstance(value, str) or not SHA256_PATTERN.match(value):
        raise ValueError("artifact reference must be a lowercase sha256 digest")
    return value


def _decode_artifact_bytes(value: object) -> bytes:
    """Decode one base64 spill chunk, refusing anything larger than a frame can carry."""
    if not isinstance(value, str):
        raise ValueError("spill data must be a base64 string")
    data = base64.b64decode(value, validate=True)
    if len(data) > _ARTIFACT_CHUNK_BYTES:
        raise ValueError(f"spill chunk exceeds {_ARTIFACT_CHUNK_BYTES} bytes")
    return data


def _required_fact_field(fact: Mapping[str, object], field_name: str) -> str:
    """Read one required observer-fact identity field, failing visibly when absent."""
    value = fact.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"observer fact requires a non-empty {field_name!r}")
    return value


class HostClient:
    """Client connection to an isolated plugin host subprocess."""

    def __init__(
        self,
        plugins: tuple[HostPlugin, ...],
        *,
        workspace: Path,
        revision_id: str,
        store: SqliteStore | None = None,
        usage_callback: Callable[[str, int, int | None], SessionUsageReport] | None = None,
        budget_callback: Callable[[str, str], PluginBudget | None] | None = None,
        spawn_child_callback: Callable[[str, str, str, int, tuple[str, ...] | None, bool], str] | None = None,
        join_child_callback: Callable[[str, str, float], Mapping[str, object]] | None = None,
        cancel_child_callback: Callable[[str, str, str], bool] | None = None,
        model_query_callback: Callable[[str, str, str | None, int | None], str] | None = None,
        progress_callback: Callable[[LeaseContext, Mapping[str, object]], None] | None = None,
        artifacts_dir: Path | None = None,
        lease_fd: int | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace directory does not exist: {self.workspace}")

        if not revision_id:
            raise ValueError("revision_id must be a non-empty string")
        self.revision_id = revision_id
        self._store = store
        self._usage_callback = usage_callback
        self._budget_callback = budget_callback
        self._spawn_child_callback = spawn_child_callback
        self._join_child_callback = join_child_callback
        self._cancel_child_callback = cancel_child_callback
        self._model_query_callback = model_query_callback
        self._progress_callback = progress_callback
        self._artifacts = None if artifacts_dir is None else ArtifactStore(artifacts_dir)

        self._context_owners: tuple[str, ...] = ()
        self._observer_owners: dict[str, tuple[str, ...]] = {}
        self._post_tool_hooks: tuple[tuple[str, tuple[str, ...], bool], ...] = ()
        self._pre_tool_hooks: tuple[tuple[str, tuple[str, ...], bool], ...] = ()
        self._lifecycle_hooks: tuple[tuple[str, str], ...] = ()
        # Client admission checks
        seen_pids: set[str] = set()
        seen_tools: set[str] = set()
        seen_commands: set[str] = set()
        seen_providers: set[str] = set()
        seen_modules: set[str] = set()

        for plugin in plugins:
            pid = plugin.manifest.plugin_id
            if pid in seen_pids:
                raise ValueError(f"Duplicate plugin id: {pid}")
            seen_pids.add(pid)

            # Check entrypoint and module collision using the top-level package name. An
            # `__init__` entrypoint is loaded as a per-plugin package under a unique name the
            # host derives from the plugin id, so it shares no top-level name with any other
            # plugin; only a package-qualified entrypoint can collide.
            if ":" not in plugin.manifest.entrypoint:
                raise ValueError(f"Invalid entrypoint format: {plugin.manifest.entrypoint}")
            mod_full = plugin.manifest.entrypoint.split(":", 1)[0]
            top_mod = mod_full.split(".", 1)[0]
            if mod_full != "__init__":
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
            for cmd_decl in plugin.manifest.commands:
                req_caps = frozenset(cmd_decl.capabilities)
                if not req_caps.issubset(plugin.capabilities):
                    missing = req_caps - plugin.capabilities
                    raise ValueError(f"Plugin {pid} command {cmd_decl.name} requires capabilities {set(missing)} not granted to plugin")
                if cmd_decl.name in seen_commands:
                    raise ValueError(f"Duplicate command name declared across plugins: {cmd_decl.name}")
                seen_commands.add(cmd_decl.name)

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
        self._shutdown: bool = False
        self._lease_fd = lease_fd
        self._active_leases: dict[str, LeaseContext] = {}
        self._lease_lock = threading.Lock()

        self._spawn_process()

        # Perform init handshake
        try:
            self._init_host()
        except Exception:
            self._kill_and_reap()
            raise

    def _spawn_process(self) -> None:
        """Spawn the plugin host process in its own session and start its reader thread."""
        pass_fds = () if self._lease_fd is None else (self._lease_fd,)
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
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"host-client-reader-{self._pid}",
        )
        self._reader_thread.start()

    def _ensure_process(self) -> None:
        """Guarantee a live initialized host, respawning after a cancellation kill.

        A cancelled or timed-out invocation kills the host process as its safety
        valve; the durable session must remain usable, so the next operation
        respawns and re-initializes an equivalent host instead of failing forever.
        """
        with self._lifecycle_lock:
            if self._shutdown:
                raise HostUnavailable("Plugin host is closed", execution_stopped=True)
            proc = self._proc
            if proc is not None and proc.poll() is None and not self._closed:
                return
            self._closed = False
            self._spawn_process()
        self._init_host()

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def tools(self) -> tuple[ToolDefinition, ...]:
        return self._tools

    @property
    def providers(self) -> tuple[str, ...]:
        return self._providers

    @property
    def commands(self) -> tuple[CommandDescriptor, ...]:
        return self._commands

    @property
    def views(self) -> tuple[str, ...]:
        return self._views

    def owner(self, tool_name: str) -> str:
        if tool_name not in self._tool_owners:
            raise KeyError(f"Tool {tool_name} not found in catalog")
        return self._tool_owners[tool_name]

    def capabilities(self, tool_name: str) -> frozenset[str]:
        if tool_name not in self._tool_capabilities:
            raise KeyError(f"Tool {tool_name} not found in catalog")
        return self._tool_capabilities[tool_name]

    def view_owner(self, view_name: str) -> str:
        if view_name not in self._view_owners:
            raise KeyError(f"View {view_name} not found in catalog")
        return self._view_owners[view_name]

    def _store_window_projection(
        self,
        lease: LeaseContext,
        covered_from_sequence: int,
        covered_to_sequence: int,
        replacement: str,
    ) -> None:
        """Verify a declared projection against durable truth, then record the measured accounting.

        The kernel owns the numbers: coverage must be a committed prefix of this session that ends
        a completed native batch (so no tool call is separated from its report), and the covered
        bytes are measured by encoding those events. A projection that would not shrink the
        window is refused rather than stored.
        """
        store = self._store
        if store is None:
            raise RuntimeError("Store not attached to HostClient")
        if not replacement.strip():
            raise ValueError("a window projection must carry non-empty replacement content")
        events = store.events(lease.session_id)
        if not events:
            raise ValueError("session has no events to project")
        last_sequence = events[-1].sequence
        if covered_from_sequence != events[0].sequence:
            raise ValueError("a window projection must start at the session's first event")
        if covered_to_sequence < covered_from_sequence or covered_to_sequence > last_sequence:
            raise ValueError(f"covered_to_sequence must be within 1..{last_sequence}")
        covered = [event for event in events if event.sequence <= covered_to_sequence]
        # Integrity: the replaced prefix must be batch-complete, so every assistant tool call it
        # covers is answered inside it and the surviving tail still starts at a message boundary.
        calls_covered = 0
        results_covered = 0
        for event in covered:
            if event.event_type == "kernel.model_turn":
                result = dict(event.payload).get("result")
                calls = result.get("tool_calls") if isinstance(result, Mapping) else None
                calls_covered += len(calls) if isinstance(calls, (list, tuple)) else 0
            elif event.event_type in ("kernel.tool_completed", "kernel.tool_interrupted"):
                results_covered += 1
        if calls_covered != results_covered:
            raise ValueError("a window projection must cover whole native tool batches")
        covered_bytes = sum(len(canonical_json(dict(event.payload))) for event in covered)
        replacement_bytes = len(replacement.encode("utf-8"))
        if replacement_bytes >= covered_bytes:
            raise ValueError("a window projection must be smaller than the window it replaces")
        store.set_window_projection(
            lease.session_id,
            lease.owner,
            covered_from_sequence=covered_from_sequence,
            covered_to_sequence=covered_to_sequence,
            replacement=replacement,
            covered_bytes=covered_bytes,
            dropped_bytes=covered_bytes - replacement_bytes,
        )

    def _capabilities_of_owner(self, owner: str) -> frozenset[str]:
        for plugin in self._plugins:
            if plugin.manifest.plugin_id == owner:
                return plugin.capabilities
        return frozenset()

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
        self._commands = tuple(decode_command_descriptor(c) for c in result.get("commands", ()))
        self._command_map = {c.name: c for c in self._commands}
        self._views = tuple(str(v) for v in result.get("views", ()))
        self._view_owners = dict(result.get("view_owners", {}))
        self._context_owners = tuple(result.get("context_owners", ()))
        self._post_tool_hooks = tuple(
            (
                str(entry["owner"]),
                tuple(str(pattern) for pattern in entry.get("match", ("*",))),
                bool(entry.get("mutating", False)),
            )
            for entry in result.get("post_tool_hooks", ())
            if isinstance(entry, Mapping)
        )
        self._pre_tool_hooks = tuple(
            (
                str(entry["owner"]),
                tuple(str(pattern) for pattern in entry.get("match", ("*",))),
                bool(entry.get("mutating", False)),
            )
            for entry in result.get("pre_tool_hooks", ())
            if isinstance(entry, Mapping)
        )
        self._lifecycle_hooks = tuple(
            (owner, kind) for owner, _, kind in (str(entry).partition("\0") for entry in result.get("lifecycle_hooks", ())) if owner and kind
        )
        obs_map = result.get("observer_owners", {})
        self._observer_owners = {}
        if isinstance(obs_map, dict):
            for k, v in obs_map.items():
                if isinstance(v, (list, tuple)):
                    self._observer_owners[str(k)] = tuple(str(x) for x in v)

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
                # Check if this is a reverse RPC request from the server
                if "method" in message:
                    self._handle_reverse_rpc(message)
                    continue

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

    #: A hook lease observes one settled moment: it may read, never mutate durable state.
    #: Anything else is refused loudly, because a silent drop is worse than a refusal.
    _HOOK_LEASE_METHODS: frozenset[str] = frozenset(
        {
            "state_get",
            "state_schema_version",
            "events_read",
            "child_events_read",
            "session_info",
            "get_usage",
            "get_budget",
            "lease_stance",
            "artifact_read",
        }
    )

    def _execute_lease_rpc(self, method: str, params: dict[str, Any], rpc_id: str, lease: LeaseContext) -> None:
        """Run one reverse RPC; each mutation re-validates the lease under the lock."""
        try:
            if lease.hook and method not in self._HOOK_LEASE_METHODS:
                # A hook runs inside the invocation it observes: it can neither stage durable
                # facts (they would be dropped, since hooks commit no state) nor start nested
                # governed work. Observers and tools are the channels that own durable writes.
                raise PermissionError(f"hook leases cannot call {method}: hooks observe, they do not stage state or start work")
            scope = str(params.get("scope", "session"))
            if scope not in ("session", "workspace"):
                raise ValueError(f"Invalid scope: {scope!r}")
            scope_key = lease.session_id if scope == "session" else str(lease.workspace)

            if method == "artifact_spill":
                # Spilling writes the kernel's own store, never the workspace or the machine: a
                # read-only lease may spill output it cannot otherwise carry, but a hook lease is
                # an observer of a settled moment and has no result to carry bytes into.
                if lease.hook:
                    raise PermissionError("hook leases cannot spill artifacts: hooks observe, they produce no result")
                store = self._require_artifacts()
                try:
                    data = _decode_artifact_bytes(params.get("data"))
                except ValueError as exc:
                    raise ValueError(f"artifact spill payload is not valid base64: {exc}") from exc
                spill_id_raw = params.get("spill_id")
                if spill_id_raw is None:
                    spill_id, spill = store.start_spill(normalize_media_type(params.get("media_type")), owner=lease.token)
                else:
                    spill_id = str(spill_id_raw)
                    spill = store.spill(spill_id)
                try:
                    spill.append(data)
                    if params.get("final"):
                        digest, size, media_type = store.finish(spill_id)
                        resp = {"sha256": digest, "bytes": size, "media_type": media_type}
                    else:
                        resp = {"spill_id": spill_id, "bytes": spill.written}
                except Exception:
                    store.discard(spill_id)
                    raise

            elif method == "artifact_read":
                sha256 = _require_sha256(params.get("sha256"))
                offset = _require_int(params.get("offset", 0), "offset")
                data, total, eof, media_type = self._require_artifacts().read(
                    sha256,
                    offset=offset,
                    limit=_require_int(params.get("limit", 65536), "limit"),
                )
                resp = {
                    "sha256": sha256,
                    "offset": offset,
                    "total_bytes": total,
                    "data": base64.b64encode(data).decode("ascii"),
                    "eof": eof,
                    "media_type": media_type,
                }

            elif method == "lease_stance":
                # The host enforces what only the kernel knows: how restrictive this lease is.
                # It answers about the presented lease and nothing else, so it carries no identity.
                # The session is what partitions supervised processes, so it travels with the
                # stance: a session can never read or stop another session's logs.
                resp = {"hook": lease.hook, "readonly": lease.readonly, "session_id": lease.session_id}

            elif method == "state_get":
                staged = lease.staged_state.get((scope, scope_key))
                if staged is not None:
                    st, ver, _baseline = staged
                else:
                    if self._store is None:
                        raise RuntimeError("Store not attached to HostClient")
                    st, ver = self._store.plugin_state(lease.owner, scope, scope_key)
                resp = {"state": json_wire_object(st) if st is not None else None, "version": ver}

            elif method == "child_spawn":
                # A read-only owner may delegate read-only work (reviews under plan mode):
                # the child inherits the restrictive stance, so no privilege is created.
                if lease.readonly and not bool(params.get("readonly", False)):
                    raise PermissionError("a read-only lease may only spawn child work that stays read-only")
                if self._spawn_child_callback is None:
                    raise RuntimeError("child spawning is unavailable")
                raw_scope = params.get("tool_allowlist")
                if raw_scope is not None and (not isinstance(raw_scope, list) or not all(isinstance(item, str) for item in raw_scope)):
                    raise ValueError("tool_allowlist must be a list of tool names or null")
                resp = {
                    "child_session_id": self._spawn_child_callback(
                        lease.session_id,
                        str(params.get("prompt", "")),
                        str(params.get("label", "")),
                        _require_int(params.get("max_steps", 32), "max_steps"),
                        None if raw_scope is None else tuple(str(item) for item in raw_scope),
                        bool(params.get("readonly", False)) or lease.readonly,
                    )
                }

            elif method == "child_join":
                if self._join_child_callback is None:
                    raise RuntimeError("child joining is unavailable")
                raw_timeout = params.get("timeout_seconds", 300.0)
                timeout_seconds = float(raw_timeout) if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool) else 300.0
                outcome = self._join_child_callback(lease.session_id, str(params.get("child_session_id", "")), timeout_seconds)
                resp = {"outcome": json_wire_object(outcome)}

            elif method == "child_cancel":
                if self._cancel_child_callback is None:
                    raise RuntimeError("child cancellation is unavailable")
                resp = {
                    "cancelled": self._cancel_child_callback(
                        lease.session_id,
                        str(params.get("child_session_id", "")),
                        str(params.get("reason", "owner cancellation")),
                    )
                }

            elif method == "window_project":
                if lease.hook:
                    raise PermissionError("hook leases cannot project a session window")
                if lease.readonly:
                    raise PermissionError("Cannot project a session window from a read-only service lease")
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                resp = {"ok": True}
                self._store_window_projection(
                    lease,
                    _require_int(params.get("covered_from_sequence"), "covered_from_sequence"),
                    _require_int(params.get("covered_to_sequence"), "covered_to_sequence"),
                    str(params.get("replacement", "")),
                )

            elif method == "window_projection_get":
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                record = self._store.window_projection(lease.session_id)
                resp = {"projection": None if record is None else asdict(record)}

            elif method == "window_projection_clear":
                if lease.hook:
                    raise PermissionError("hook leases cannot clear a session window projection")
                if lease.readonly:
                    raise PermissionError("Cannot clear a session window projection from a read-only service lease")
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                resp = {"cleared": self._store.clear_window_projection(lease.session_id)}

            elif method == "overlay_set":
                if lease.actor != "user":
                    raise PermissionError("restrictive overlays require authentic user authority")
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                resp = {"ok": True}
                self._store.set_session_overlay(lease.session_id, lease.owner, str(params.get("tier", "read-only")))

            elif method == "overlay_clear":
                if lease.actor != "user":
                    raise PermissionError("restrictive overlays require authentic user authority")
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                resp = {"cleared": self._store.clear_session_overlay(lease.session_id, lease.owner)}

            elif method == "wakeup_schedule":
                if lease.hook:
                    raise PermissionError("post-tool hooks cannot start nested governed work")
                if lease.readonly:
                    raise PermissionError("Cannot schedule wakeups from a read-only service lease")
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                raw_due = params.get("due_at_ms")
                due_at = _require_int(raw_due, "due_at_ms")
                if due_at < 0:
                    raise ValueError("due_at_ms must be >= 0")
                raw_role = params.get("role", "system")
                if raw_role not in ("system", "user"):
                    raise ValueError("role must be 'system' or 'user'")
                raw_key = params.get("idempotency_key")
                idempotency_key = str(raw_key) if isinstance(raw_key, str) and raw_key else None
                wakeup_id = (
                    uuid4().hex
                    if idempotency_key is None
                    else hashlib.sha256("\0".join((lease.session_id, lease.owner, "wakeup", idempotency_key)).encode("utf-8")).hexdigest()
                )
                self._store.schedule_wakeup(
                    wakeup_id,
                    lease.owner,
                    str(lease.workspace),
                    lease.session_id,
                    own_json_object(params["payload"]),
                    due_at,
                    str(raw_role),
                )
                resp = {"wakeup_id": wakeup_id}

            elif method == "wakeup_cancel":
                if lease.readonly:
                    raise PermissionError("Cannot cancel wakeups from a read-only service lease")
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                resp = {"cancelled": self._store.cancel_wakeup(str(params.get("wakeup_id", "")), lease.owner)}

            elif method == "events_read":
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                after = _require_int(params.get("after_sequence", 0), "after_sequence")
                limit = _require_int(params.get("limit", 100), "limit")
                if after < 0 or limit < 1 or limit > 200:
                    raise ValueError("after_sequence must be >= 0 and limit within 1..200")
                include_payload = params.get("include_payload", True)
                if type(include_payload) is not bool:
                    raise ValueError("include_payload must be a boolean")
                resp = {"events": self._event_views(lease.session_id, after, limit, include_payload)}

            elif method == "child_events_read":
                # The kernel owns lineage, so the caller never supplies the reason it may read:
                # a child of the SAME root chain is the whole privilege.
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                child_id = params.get("child_session_id")
                if not isinstance(child_id, str) or not child_id or child_id == lease.session_id:
                    raise ValueError("child_session_id must name another session")
                after = _require_int(params.get("after_sequence", 0), "after_sequence")
                limit = _require_int(params.get("limit", 100), "limit")
                if after < 0 or limit < 1 or limit > 200:
                    raise ValueError("after_sequence must be >= 0 and limit within 1..200")
                caller = self._store.session(lease.session_id)
                child = self._store.session(child_id)
                if child.parent_session_id is None or child.root_session_id != caller.root_session_id:
                    raise PermissionError("child events are readable only within the caller's own root chain")
                include_payload = params.get("include_payload", True)
                if type(include_payload) is not bool:
                    raise ValueError("include_payload must be a boolean")
                resp = {"events": self._event_views(child_id, after, limit, include_payload)}

            elif method == "session_info":
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                resp = {"session": self._store.session_info(lease.session_id, lease.owner).as_payload()}

            elif method == "invoke_tool":
                name = params.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("invoke_tool requires a non-empty name")
                revision = params.get("expected_revision")
                if revision != self.revision_id:
                    raise ValueError("invoke_tool expected_revision differs from the active revision")
                raw_arguments = params.get("arguments", {})
                if not isinstance(raw_arguments, Mapping):
                    raise ValueError("invoke_tool arguments must be an object")
                request_id = uuid4().hex
                depth = lease.invocation_depth + 1
                call = ToolCall(name, own_json_object(raw_arguments), tool_call_id=request_id)
                checkpoint = None if self._store is None else self._store.checkpoint(lease.session_id)
                root_id = "" if checkpoint is None or checkpoint.kind == "terminal" else checkpoint.payload.get("root_id", "")
                with self._lease_lock:
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    lease.staged_events.append(
                        EventDraft(
                            "kernel.tool_deferred",
                            "kernel",
                            "plugin",
                            {
                                "request_id": request_id,
                                "call": encode_tool_call(call),
                                "expected_revision": revision,
                                "requester": lease.owner,
                                "root_id": root_id,
                                "caller_invocation_id": lease.invocation_id,
                                "depth": depth,
                            },
                        )
                    )
                resp = {"request_id": request_id, "tool_name": name, "depth": depth}

            elif method == "progress_notify":
                # A callback reporting on work still in flight. Observational only: the tool's
                # own result is what carries truth, so a failed report never changes it.
                if self._progress_callback is None:
                    raise RuntimeError("progress reporting is unavailable")
                payload = params.get("payload")
                if not isinstance(payload, Mapping):
                    raise ValueError("progress payload must be an object")
                with self._lease_lock:
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    self._progress_callback(lease, own_json_object(payload))
                resp = {"ok": True}

            elif method == "model_query":
                if self._model_query_callback is None:
                    raise RuntimeError("governed model queries are unavailable")
                raw_model = params.get("model")
                if raw_model is not None and not isinstance(raw_model, str):
                    raise ValueError("model must be a string or null")
                raw_max = params.get("max_output_tokens")
                if raw_max is not None:
                    raw_max = _require_int(raw_max, "max_output_tokens")
                resp = {"output": self._model_query_callback(lease.session_id, str(params.get("prompt", "")), raw_model, raw_max)}

            elif method == "state_schema_version":
                if self._store is None:
                    raise RuntimeError("Store not attached to HostClient")
                _payload, _version, stored_schema = self._store.plugin_state_meta(lease.owner, scope, scope_key)
                # The stored row's schema version only: 0 means "nothing stored yet", which is
                # unambiguous for an owner deciding whether it must migrate its own state.
                resp = {"schema_version": stored_schema}

            elif method == "state_replace":
                with self._lease_lock:
                    if lease.readonly:
                        raise PermissionError("Cannot modify state from a read-only service lease")
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    state = own_json_object(params["state"])
                    expected_version = _require_int(params.get("expected_version"), "expected_version")
                    staged = lease.staged_state.get((scope, scope_key))
                    if staged is not None:
                        # Exactly one future version per lease: the first write published
                        # it, later writes in the same lease must target that version.
                        _st, local_version, baseline = staged
                        if expected_version != local_version:
                            raise RuntimeError(f"state CAS conflict within lease: expected version {local_version}, got {expected_version}")
                    else:
                        if self._store is None:
                            raise RuntimeError("Store not attached to HostClient")
                        _, current = self._store.plugin_state(lease.owner, scope, scope_key)
                        if expected_version != current:
                            raise RuntimeError(f"state CAS conflict: expected version {expected_version}, got {current}")
                        baseline = current
                    local_version = baseline + 1
                    lease.staged_state[(scope, scope_key)] = (state, local_version, baseline)
                resp = {"ok": True, "version": local_version}

            elif method == "event_stage":
                event_type = str(params["event_type"])
                payload = own_json_object(params["payload"])
                with self._lease_lock:
                    if lease.readonly:
                        raise PermissionError("Cannot stage events from a read-only service lease")
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    lease.staged_events.append(
                        EventDraft(
                            event_type=f"plugin.{lease.owner}.{event_type}",
                            source=f"plugin.{lease.owner}",
                            actor="plugin",
                            payload=payload,
                        )
                    )
                resp = {"ok": True}

            elif method == "continuation_enqueue":
                payload = own_json_object(params["payload"])
                raw_role = params.get("role", "system")
                if raw_role not in ("system", "user"):
                    raise ValueError("role must be 'system' or 'user'")
                role = str(raw_role)
                raw_key = params.get("idempotency_key")
                idempotency_key = str(raw_key) if raw_key is not None else None
                with self._lease_lock:
                    if lease.readonly:
                        raise PermissionError("Cannot enqueue continuations from a read-only service lease")
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    existing_id = next(
                        (
                            c.continuation_id
                            for c in lease.staged_continuations
                            if c.idempotency_key == idempotency_key and idempotency_key is not None
                        ),
                        None,
                    )
                    existing = existing_id
                    if existing is None and idempotency_key is not None:
                        if self._store is None:
                            raise RuntimeError("Store not attached to HostClient")
                        existing = self._store.continuation_id_for_idempotency(lease.session_id, lease.owner, idempotency_key)
                    if existing is not None:
                        cid = existing
                    else:
                        # A keyed identity is derived from the key itself, so two
                        # concurrent leases that both stage before either commits
                        # return the SAME id: the store's key dedupe then keeps
                        # exactly one durable row that every caller's id names.
                        # ponytail: sha256 of identity fields; a store-side
                        # counter if these ever need to be human-sortable.
                        cid = (
                            uuid4().hex
                            if idempotency_key is None
                            else hashlib.sha256("\0".join((lease.session_id, lease.owner, idempotency_key)).encode("utf-8")).hexdigest()
                        )
                        if lease.hook:
                            raise PermissionError("post-tool hooks cannot start nested governed work")
                        lease.op_counter += 1
                        lease.staged_continuations.append(
                            StagedContinuationDraft(
                                continuation_id=cid,
                                owner=lease.owner,
                                source=f"plugin.{lease.owner}",
                                payload=payload,
                                idempotency_key=idempotency_key,
                                ordinal=lease.op_counter,
                                role=role,
                            )
                        )
                resp = {"continuation_id": cid}

            elif method == "continuations_cancel":
                cid = params.get("continuation_id")
                with self._lease_lock:
                    if lease.readonly:
                        raise PermissionError("Cannot cancel continuations from a read-only service lease")
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    lease.op_counter += 1
                    lease.staged_cancellations.append(
                        StagedCancellation(
                            owner=lease.owner,
                            continuation_id=str(cid) if cid is not None else None,
                            ordinal=lease.op_counter,
                        )
                    )
                resp = {"ok": True}

            elif method == "get_usage":
                since_sequence = _require_int(params.get("since_sequence"), "since_sequence")
                until_raw = params.get("until_sequence")
                until_sequence = None if until_raw is None else _require_int(until_raw, "until_sequence")
                if self._usage_callback is None:
                    raise RuntimeError("usage service is unavailable in this host")
                resp = self._usage_callback(lease.session_id, since_sequence, until_sequence).as_payload()

            elif method == "get_budget":
                with self._lease_lock:
                    staged_budget = lease.staged_budget
                if staged_budget == "clear":
                    resp = {"budget": None}
                elif staged_budget is not None:
                    resp = {"budget": staged_budget.as_payload()}
                else:
                    if self._budget_callback is None:
                        raise RuntimeError("budget service is unavailable in this host")
                    b = self._budget_callback(lease.session_id, lease.owner)
                    resp = {"budget": b.as_payload() if b is not None else None}

            elif method == "set_budget":
                since_sequence = _require_int(params.get("since_sequence"), "since_sequence")
                budget = PluginBudget(
                    owner=lease.owner,
                    since_sequence=since_sequence,
                    max_input_tokens=params.get("max_input_tokens"),
                    max_output_tokens=params.get("max_output_tokens"),
                    max_total_tokens=params.get("max_total_tokens"),
                    max_cost_usd=params.get("max_cost_usd"),
                    max_elapsed_seconds=params.get("max_elapsed_seconds"),
                )
                with self._lease_lock:
                    if lease.readonly:
                        raise PermissionError("Cannot set budget from a read-only service lease")
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    lease.staged_budget = budget
                resp = {"ok": True}

            elif method == "clear_budget":
                with self._lease_lock:
                    if lease.readonly:
                        raise PermissionError("Cannot clear budget from a read-only service lease")
                    if lease.revoked:
                        raise PermissionError("Service lease was revoked")
                    lease.staged_budget = "clear"
                resp = {"ok": True}

            else:
                self._send_request(
                    {
                        "protocol_version": 1,
                        "id": rpc_id,
                        "ok": False,
                        "error": {"kind": "unknown_rpc", "message": f"Unknown reverse RPC method: {method}"},
                    }
                )
                return

            self._send_request(
                {
                    "protocol_version": 1,
                    "id": rpc_id,
                    "ok": True,
                    "result": json_wire_object(resp),
                }
            )
        except Exception as exc:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": rpc_id,
                    "ok": False,
                    "error": {"kind": "invalid_params", "message": f"Service call rejected: {type(exc).__name__}: {exc}"},
                }
            )

    def _event_views(self, session_id: str, after: int, limit: int, include_payload: bool) -> list[dict[str, object]]:
        """One bounded page of a session's events; the payload budget is shared by the page."""
        if self._store is None:
            raise RuntimeError("Store not attached to HostClient")
        views: list[dict[str, object]] = []
        budget = 64 * 1024
        for event in self._store.events(session_id, after_sequence=after, limit=limit):
            payload_wire: dict[str, object] | None = None
            truncated = False
            if include_payload:
                serialized = json.dumps(json_wire_object(event.payload), ensure_ascii=False, allow_nan=False)
                if len(serialized.encode("utf-8")) > budget:
                    truncated = True
                    payload_wire = {"truncated": True, "original_bytes": len(serialized.encode("utf-8"))}
                else:
                    budget -= len(serialized.encode("utf-8"))
                    payload_wire = json_wire_object(event.payload)
            views.append(
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "run_id": event.run_id,
                    "revision_id": event.revision_id,
                    "source": event.source,
                    "actor": event.actor,
                    "payload": payload_wire,
                    "payload_truncated": truncated,
                }
            )
        return views

    def _handle_reverse_rpc(self, message: dict[str, Any]) -> None:
        rpc_id = str(message.get("id", ""))
        method = message.get("method")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return

        def execute_rpc() -> None:
            try:
                if not isinstance(method, str) or not method:
                    raise PermissionError("Reverse RPC requires a non-empty method name")
                # 1. Reject any request attempting to spoof owner, actor, or scope_key
                for forbidden in ("owner", "actor", "scope_key"):
                    if forbidden in params:
                        raise PermissionError(f"Reverse RPC parameter '{forbidden}' is forbidden; identity is governed by lease token")

                # 2. Authenticate lease token
                token = params.get("lease_token")
                if not token or not isinstance(token, str):
                    raise PermissionError("Missing required 'lease_token' in reverse RPC")

                with self._lease_lock:
                    lease = self._active_leases.get(token)
                if lease is None or lease.revoked:
                    raise PermissionError("Invalid or expired service lease token")
                self._execute_lease_rpc(method, params, rpc_id, lease)
            except Exception as exc:
                self._send_request(
                    {
                        "protocol_version": 1,
                        "id": rpc_id,
                        "ok": False,
                        "error": {"kind": "invalid_params", "message": f"Service call rejected: {type(exc).__name__}: {exc}"},
                    }
                )

        threading.Thread(target=execute_rpc, daemon=True).start()

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

    def _release_artifacts(self) -> None:
        if self._artifacts is not None:
            self._artifacts.discard_all()

    def _require_artifacts(self) -> ArtifactStore:
        if self._artifacts is None:
            raise RuntimeError("the kernel did not attach an artifact store to this host")
        return self._artifacts

    def state_schema_version(self, owner: str) -> int:
        for plugin in self._plugins:
            if plugin.manifest.plugin_id == owner:
                return getattr(plugin.manifest, "state_schema_version", 1)
        return 1

    @property
    def loaded_plugins(self) -> tuple[HostPlugin, ...]:
        """The plugin sets this host actually loaded, in admission order."""
        return tuple(self._plugins)

    def migrate_owner_state(self, owner: str, payload: Mapping[str, object], *, from_version: int, to_version: int) -> dict[str, Any]:
        """Run one owner's pure old->new state transform in this host; no lease, no services."""
        return self._pure_transform(
            "state_migrate",
            "mig",
            {
                "owner": owner,
                "payload": json_wire_object(payload),
                "from_version": from_version,
                "to_version": to_version,
            },
        )

    def prepare_deactivate_owner_state(
        self,
        owner: str,
        payload: Mapping[str, object],
        *,
        scope: str,
        scope_key: str,
        tip_sequence: int | None,
    ) -> dict[str, Any]:
        """Run one owner's pure deactivation transform in this host; no lease, no services."""
        return self._pure_transform(
            "state_prepare_deactivate",
            "deact",
            {
                "owner": owner,
                "payload": json_wire_object(payload),
                "scope": scope,
                "scope_key": scope_key,
                "tip_sequence": tip_sequence,
            },
        )

    def _pure_transform(self, method: str, prefix: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a kernel-owned pure transform request and return the payload it produced."""
        self._ensure_process()
        req_id = self._next_id(prefix)
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future
        try:
            self._send_request({"protocol_version": 1, "id": req_id, "method": method, "params": params})
            response = self._wait_for_future(req_id, future, 120.0, None)
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send {method} request: {exc}", execution_stopped=stopped) from exc
        finally:
            with self._maps_lock:
                self._pending.pop(req_id, None)
        if not response.get("ok", False):
            err = response.get("error", {})
            raise RuntimeError(err.get("message", f"{method} failed") if isinstance(err, dict) else f"{method} failed")
        result = response.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("payload"), dict):
            raise HostUnavailable(f"{method} returned a malformed result", execution_stopped=False)
        return result["payload"]

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        timeout_seconds: float = 120,
        return_outcome: bool = False,
        readonly: bool = False,
    ) -> ToolResult | ToolInvocationOutcome:
        """Invoke a registered tool in the host process."""
        self._ensure_process()

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

        tool_owner = self.owner(call.tool_name)
        tool_desc = self._tool_map[call.tool_name]
        # A tool lease is restricted when the session's effective stance is restrictive,
        # even if the tool's own effects are read-tier or spawn-tier.
        is_ro = readonly or is_read_tier(tool_desc.effects)
        lease_token = uuid4().hex
        lease = LeaseContext(
            token=lease_token,
            owner=tool_owner,
            session_id=context.session_id,
            workspace=self.workspace,
            run_id=context.run_id,
            revision_id=self.revision_id,
            invocation_id=context.invocation_id,
            grants=context.capabilities,
            readonly=is_ro,
            actor=context.actor,
            invocation_depth=context.invocation_depth,
        )
        with self._lease_lock:
            self._active_leases[lease_token] = lease

        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "invoke",
                    "params": {
                        "call": encode_tool_call(call),
                        "context": encode_context(context),
                        "lease_token": lease_token,
                    },
                }
            )
            response = self._wait_for_future(req_id, future, timeout_seconds, context.abort_signal)
        except HostTimeout, InvocationCancelled:
            raise
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send invoke request: {exc}", execution_stopped=stopped) from exc
        finally:
            with self._lease_lock:
                lease.revoked = True
                self._active_leases.pop(lease_token, None)
            if self._artifacts is not None:
                self._artifacts.discard_owner(lease_token)

        if not response.get("ok", False):
            err = response.get("error", {})
            raise RuntimeError(err.get("message", "Invocation failed") if isinstance(err, dict) else "Invocation failed")

        result_payload = response.get("result", {})
        raw_tool_res = result_payload.get("tool_result", result_payload) if isinstance(result_payload, dict) else result_payload
        result = decode_tool_result(raw_tool_res)
        threw = bool(result_payload.get("threw", False)) if isinstance(result_payload, dict) else False
        cancelled = bool(context.abort_signal and context.abort_signal.cancelled)

        if threw or cancelled:
            staged_state_updates = ()
            staged_events = ()
            staged_continuations = ()
            staged_cancellations = ()
            staged_budget = None
        else:
            owner_schema_ver = self.state_schema_version(lease.owner)
            staged_state_updates = tuple(
                StagedStateUpdate(
                    owner=lease.owner,
                    scope=sc,
                    scope_key=sk,
                    payload=st,
                    expected_version=baseline,
                    schema_version=owner_schema_ver,
                    revision_id=self.revision_id,
                )
                for (sc, sk), (st, _local, baseline) in lease.staged_state.items()
            )
            staged_events = tuple(lease.staged_events)
            staged_continuations = tuple(lease.staged_continuations)
            staged_cancellations = tuple(lease.staged_cancellations)
            staged_budget = lease.staged_budget

        outcome = ToolInvocationOutcome(
            result=result,
            staged_state=staged_state_updates,
            staged_events=staged_events,
            staged_continuations=staged_continuations,
            staged_cancellations=staged_cancellations,
            staged_budget=staged_budget,
        )
        if return_outcome:
            return outcome
        return result

    def propose_turn(
        self,
        request: ProviderTurnRequest,
        *,
        provider: str = "openai-chat",
        timeout_seconds: float = 120,
    ) -> ProviderTurnResult:
        """Request a model turn from a registered provider."""
        self._ensure_process()

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

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object],
        *,
        context: ToolContext,
        client_capabilities: frozenset[str] = frozenset(),
        timeout_seconds: float = 120,
    ) -> tuple[
        CommandResult,
        tuple[StagedStateUpdate, ...],
        tuple[EventDraft, ...],
        tuple[StagedContinuationDraft, ...],
        tuple[StagedCancellation, ...],
        PluginBudget | None | Literal["clear"],
    ]:
        """Invoke a registered command in the host process."""
        self._ensure_process()

        cmd_desc = self._command_map.get(name)
        if cmd_desc is None:
            raise ValueError(f"Unknown command: {name}")
        req_id = self._next_id("cmd")
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future

        lease_token = uuid4().hex
        lease = LeaseContext(
            token=lease_token,
            owner=cmd_desc.owner,
            session_id=session_id,
            workspace=self.workspace,
            run_id=context.run_id,
            revision_id=self.revision_id,
            invocation_id=context.invocation_id,
            grants=context.capabilities,
            readonly=not any(e in (ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.SPAWN, ToolEffect.SESSION) for e in cmd_desc.effects),
            actor=context.actor,
        )
        with self._lease_lock:
            self._active_leases[lease_token] = lease

        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "invoke_command",
                    "params": {
                        "name": name,
                        "arguments": json_wire_object(arguments),
                        "context": encode_context(context),
                        "lease_token": lease_token,
                        "client_capabilities": sorted(list(client_capabilities)),
                    },
                }
            )
            response = self._wait_for_future(req_id, future, timeout_seconds, context.abort_signal)
        except HostTimeout, InvocationCancelled:
            raise
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send invoke_command request: {exc}", execution_stopped=stopped) from exc
        finally:
            with self._lease_lock:
                lease.revoked = True
                self._active_leases.pop(lease_token, None)
            if self._artifacts is not None:
                self._artifacts.discard_owner(lease_token)
        if not response.get("ok", False):
            err = response.get("error", {})
            raise RuntimeError(err.get("message", "Command invocation failed") if isinstance(err, dict) else "Command invocation failed")

        result_payload = response.get("result", {})
        cmd_result = decode_command_result(result_payload["command_result"])

        threw = bool(response.get("threw", False))
        cancelled = bool(context.abort_signal and context.abort_signal.cancelled)
        if threw or cancelled:
            staged_state_updates = ()
            staged_events = ()
            staged_continuations = ()
            staged_cancellations = ()
            staged_budget = None
        else:
            owner_schema_ver = self.state_schema_version(lease.owner)
            staged_state_updates = tuple(
                StagedStateUpdate(
                    owner=lease.owner,
                    scope=sc,
                    scope_key=sk,
                    payload=st,
                    expected_version=baseline,
                    schema_version=owner_schema_ver,
                    revision_id=self.revision_id,
                )
                for (sc, sk), (st, _local, baseline) in lease.staged_state.items()
            )
            staged_events = tuple(lease.staged_events)
            staged_continuations = tuple(lease.staged_continuations)
            staged_cancellations = tuple(lease.staged_cancellations)
            staged_budget = lease.staged_budget
        return cmd_result, staged_state_updates, staged_events, staged_continuations, staged_cancellations, staged_budget

    def render_view(
        self,
        session_id: str,
        name: str,
        *,
        run_id: str = "",
        client_capabilities: frozenset[str] = frozenset(),
        timeout_seconds: float = 30,
    ) -> PluginViewDescriptor:
        """Render a declarative plugin view for a session."""
        self._ensure_process()

        if name not in self._views:
            raise ValueError(f"Unknown view: {name}")

        req_id = self._next_id("view")
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future

        lease_token = uuid4().hex
        view_owner = self.view_owner(name)
        lease = LeaseContext(
            token=lease_token,
            owner=view_owner,
            session_id=session_id,
            workspace=self.workspace,
            run_id=run_id,
            revision_id=self.revision_id,
            invocation_id=uuid4().hex,
            grants=frozenset(),
            readonly=True,
            actor="plugin",
        )
        with self._lease_lock:
            self._active_leases[lease_token] = lease

        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "render_view",
                    "params": {
                        "name": name,
                        "session_id": session_id,
                        "run_id": run_id,
                        "invocation_id": lease.invocation_id,
                        "lease_token": lease_token,
                        "client_capabilities": sorted(list(client_capabilities)),
                    },
                }
            )
            response = self._wait_for_future(req_id, future, timeout_seconds, None)
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send render_view request: {exc}", execution_stopped=stopped) from exc
        finally:
            with self._lease_lock:
                self._active_leases.pop(lease_token, None)

        if not response.get("ok", False):
            err = response.get("error", {})
            raise RuntimeError(err.get("message", "View rendering failed") if isinstance(err, dict) else "View rendering failed")

        return decode_view_descriptor(response["result"])

    @property
    def lifecycle_hooks(self) -> tuple[tuple[str, str], ...]:
        """Declared lifecycle hooks: (owner, kind) pairs, kinds session_start/session_end."""
        return self._lifecycle_hooks

    @property
    def pre_tool_hooks(self) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        """Declared pre-tool gates: (owner, tool match patterns, mutating)."""
        return self._pre_tool_hooks

    @property
    def post_tool_hooks(self) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        """Declared post-tool hooks: (owner, tool match patterns, mutating)."""
        return self._post_tool_hooks

    def run_lifecycle(
        self,
        owner: str,
        kind: str,
        *,
        session_id: str,
        run_id: str,
        logical_turn: str = "",
        status: str = "",
        error: str | None = None,
        readonly: bool = True,
        timeout_seconds: float = 30,
    ) -> str | None:
        """Notify one owner's lifecycle hook; returns its note (never a decision)."""
        self._ensure_process()
        req_id = self._next_id("life")
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future
        lease_token = uuid4().hex
        invocation_id = uuid4().hex
        lease = LeaseContext(
            token=lease_token,
            owner=owner,
            session_id=session_id,
            workspace=self.workspace,
            run_id=run_id,
            revision_id=self.revision_id,
            invocation_id=invocation_id,
            grants=self._capabilities_of_owner(owner),
            readonly=readonly,
            actor="plugin",
            hook=True,
        )
        with self._lease_lock:
            self._active_leases[lease_token] = lease
        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "run_lifecycle",
                    "params": {
                        "owner": owner,
                        "kind": kind,
                        "session_id": session_id,
                        "run_id": run_id,
                        "invocation_id": invocation_id,
                        "lease_token": lease_token,
                        "logical_turn": logical_turn,
                        "status": status,
                        "error": error,
                    },
                }
            )
            response = self._wait_for_future(req_id, future, timeout_seconds, None)
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send run_lifecycle request: {exc}", execution_stopped=stopped) from exc
        finally:
            with self._lease_lock:
                self._active_leases.pop(lease_token, None)

        if not response.get("ok", False):
            error_info = response.get("error", {})
            message = error_info.get("message", "lifecycle hook failed") if isinstance(error_info, Mapping) else "lifecycle hook failed"
            raise RuntimeError(str(message))
        note = response.get("result", {}).get("note")
        return note if isinstance(note, str) else None

    def pre_tool(
        self,
        owner: str,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        arguments: Mapping[str, object],
        readonly: bool,
        logical_turn: str = "",
        timeout_seconds: float = 30,
    ) -> str | None:
        """Ask one owner's pre-tool gate whether this call may run.

        Returns a denial reason, or None to allow. The gate observes the intent: it cannot
        rewrite the call, and a failure on this path is the caller's to treat as a refusal.
        """
        self._ensure_process()
        req_id = self._next_id("gate")
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future
        lease_token = uuid4().hex
        invocation_id = uuid4().hex
        lease = LeaseContext(
            token=lease_token,
            owner=owner,
            session_id=session_id,
            workspace=self.workspace,
            run_id=run_id,
            revision_id=self.revision_id,
            invocation_id=invocation_id,
            grants=self._capabilities_of_owner(owner),
            readonly=readonly,
            actor="plugin",
            hook=True,
        )
        with self._lease_lock:
            self._active_leases[lease_token] = lease
        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "pre_tool",
                    "params": {
                        "owner": owner,
                        "session_id": session_id,
                        "run_id": run_id,
                        "invocation_id": invocation_id,
                        "lease_token": lease_token,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": json_wire_object(arguments),
                        "logical_turn": logical_turn,
                    },
                }
            )
            response = self._wait_for_future(req_id, future, timeout_seconds, None)
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send pre_tool request: {exc}", execution_stopped=stopped) from exc
        finally:
            with self._lease_lock:
                self._active_leases.pop(lease_token, None)

        if not response.get("ok", False):
            error = response.get("error", {})
            message = error.get("message", "pre-tool gate failed") if isinstance(error, Mapping) else "pre-tool gate failed"
            raise RuntimeError(str(message))
        reason = response.get("result", {}).get("reason")
        return reason if isinstance(reason, str) else None

    def post_tool(
        self,
        owner: str,
        *,
        session_id: str,
        run_id: str,
        tool_name: str,
        tool_call_id: str | None,
        status: str,
        text: str,
        arguments: Mapping[str, object],
        readonly: bool,
        logical_turn: str = "",
        timeout_seconds: float = 30,
    ) -> str | None:
        """Run one owner's post-tool hook inside the governed invocation.

        The hook observes a settled result under its own lease: it cannot start nested
        governed work, and its failure is the caller's to record, never a rewrite of the
        tool verdict this invocation already decided.
        """
        self._ensure_process()
        req_id = self._next_id("hook")
        future: Future[dict[str, Any]] = Future()
        with self._maps_lock:
            self._pending[req_id] = future
        lease_token = uuid4().hex
        invocation_id = uuid4().hex
        lease = LeaseContext(
            token=lease_token,
            owner=owner,
            session_id=session_id,
            workspace=self.workspace,
            run_id=run_id,
            revision_id=self.revision_id,
            invocation_id=invocation_id,
            grants=self._capabilities_of_owner(owner),
            readonly=readonly,
            actor="plugin",
            hook=True,
        )
        with self._lease_lock:
            self._active_leases[lease_token] = lease
        try:
            self._send_request(
                {
                    "protocol_version": 1,
                    "id": req_id,
                    "method": "post_tool",
                    "params": {
                        "owner": owner,
                        "session_id": session_id,
                        "run_id": run_id,
                        "invocation_id": invocation_id,
                        "lease_token": lease_token,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "status": status,
                        "text": text,
                        "arguments": json_wire_object(arguments),
                        "logical_turn": logical_turn,
                    },
                }
            )
            response = self._wait_for_future(req_id, future, timeout_seconds, None)
        except Exception as exc:
            with self._maps_lock:
                self._pending.pop(req_id, None)
            stopped = self._kill_and_reap()
            raise HostUnavailable(f"Failed to send post_tool request: {exc}", execution_stopped=stopped) from exc
        finally:
            with self._lease_lock:
                self._active_leases.pop(lease_token, None)

        if not response.get("ok", False):
            error = response.get("error", {})
            message = error.get("message", "post-tool hook failed") if isinstance(error, Mapping) else "post-tool hook failed"
            raise RuntimeError(str(message))
        note = response.get("result", {}).get("note")
        return note if isinstance(note, str) else None

    def contribute_context(
        self,
        session_id: str,
        *,
        run_id: str = "",
        logical_turn: str = "",
        timeout_seconds: float = 30,
    ) -> tuple[dict[str, object], ...]:
        # A dead or closing host is a critical admission failure: the caller
        # finishes the run failed instead of silently dropping contributed
        # context and dispatching without it.
        self._ensure_process()
        all_contributions: list[dict[str, object]] = []
        for owner in self._context_owners:
            req_id = self._next_id("ctx")
            future: Future[dict[str, Any]] = Future()
            with self._maps_lock:
                self._pending[req_id] = future

            lease_token = uuid4().hex
            lease = LeaseContext(
                token=lease_token,
                owner=owner,
                session_id=session_id,
                workspace=self.workspace,
                run_id=run_id,
                revision_id=self.revision_id,
                invocation_id=uuid4().hex,
                grants=frozenset(),
                readonly=True,
                actor="plugin",
            )
            with self._lease_lock:
                self._active_leases[lease_token] = lease

            try:
                self._send_request(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "method": "contribute_context",
                        "params": {
                            "session_id": session_id,
                            "owner": owner,
                            "run_id": run_id,
                            "logical_turn": logical_turn,
                            "invocation_id": lease.invocation_id,
                            "lease_token": lease_token,
                        },
                    }
                )
                response = self._wait_for_future(req_id, future, timeout_seconds, None)
            except Exception as exc:
                with self._maps_lock:
                    self._pending.pop(req_id, None)
                stopped = self._kill_and_reap()
                raise HostUnavailable(f"Failed to send contribute_context request: {exc}", execution_stopped=stopped) from exc
            finally:
                with self._lease_lock:
                    self._active_leases.pop(lease_token, None)

            if not response.get("ok", False):
                err = response.get("error", {})
                raise RuntimeError(err.get("message", "Context contribution failed") if isinstance(err, dict) else "Context contribution failed")

            contributions = response.get("result", {}).get("contributions", [])
            if isinstance(contributions, list):
                all_contributions.extend(contributions)

        return tuple(all_contributions)

    def notify_fact(
        self,
        fact_type: str,
        fact: Mapping[str, object],
        *,
        owners: Sequence[str] | None = None,
        logical_turn: str = "",
        timeout_seconds: float = 30,
    ) -> CallbackChanges:
        """Notify subscribers of one fact; `owners` restricts delivery to those captured subscribers."""
        subscribed_owners = self._observer_owners.get(fact_type, ())
        if owners is not None:
            requested = frozenset(owners)
            subscribed_owners = tuple(owner for owner in subscribed_owners if owner in requested)
        if not subscribed_owners:
            return CallbackChanges()

        try:
            self._ensure_process()
        except HostUnavailable as exc:
            errors = tuple({"owner": owner, "error": f"Plugin host unavailable: {exc}"} for owner in subscribed_owners)
            return CallbackChanges(errors=errors)

        all_state_updates: list[StagedStateUpdate] = []
        all_events: list[EventDraft] = []
        all_conts: list[StagedContinuationDraft] = []
        all_cancels: list[StagedCancellation] = []
        all_budgets: list[tuple[str, PluginBudget | Literal["clear"]]] = []
        all_errors: list[dict[str, str]] = []

        session_id = _required_fact_field(fact, "session_id")
        run_id = _required_fact_field(fact, "run_id")
        revision_id = _required_fact_field(fact, "revision_id")

        for owner in subscribed_owners:
            req_id = self._next_id("obs")
            future: Future[dict[str, Any]] = Future()
            with self._maps_lock:
                self._pending[req_id] = future

            lease_token = uuid4().hex
            lease = LeaseContext(
                token=lease_token,
                owner=owner,
                session_id=session_id,
                workspace=self.workspace,
                run_id=run_id,
                revision_id=revision_id,
                invocation_id=uuid4().hex,
                grants=frozenset(),
                readonly=False,
                actor="plugin",
            )
            with self._lease_lock:
                self._active_leases[lease_token] = lease

            try:
                self._send_request(
                    {
                        "protocol_version": 1,
                        "id": req_id,
                        "method": "notify_fact",
                        "params": {
                            "fact_type": fact_type,
                            "fact": json_wire_object(fact),
                            "owner": owner,
                            "logical_turn": logical_turn,
                            "invocation_id": lease.invocation_id,
                            "lease_token": lease_token,
                        },
                    }
                )
                response = self._wait_for_future(req_id, future, timeout_seconds, None)
            except Exception as exc:
                with self._maps_lock:
                    self._pending.pop(req_id, None)
                all_errors.append({"owner": owner, "error": f"Observer communication failed: {type(exc).__name__}"})
                continue
            finally:
                with self._lease_lock:
                    lease.revoked = True
                    self._active_leases.pop(lease_token, None)

            if not response.get("ok", False):
                err = response.get("error", {})
                all_errors.append({"owner": owner, "error": str(err.get("message", "Observer call failed"))})
                continue
            raw_errs = response.get("result", {}).get("errors", [])
            raised_here = [e for e in raw_errs if isinstance(e, dict) and e.get("owner") == owner] if isinstance(raw_errs, list) else []
            if raised_here:
                # A raised observer callback invalidates every proposal it staged:
                # truth (bill/fact) stays, its partial side effects do not.
                all_errors.extend(raised_here)
                continue

            owner_schema_ver = self.state_schema_version(lease.owner)
            all_state_updates.extend(
                StagedStateUpdate(
                    owner=lease.owner,
                    scope=sc,
                    scope_key=sk,
                    payload=st,
                    expected_version=baseline,
                    schema_version=owner_schema_ver,
                    revision_id=self.revision_id,
                )
                for (sc, sk), (st, _local, baseline) in lease.staged_state.items()
            )
            all_events.extend(lease.staged_events)
            all_conts.extend(lease.staged_continuations)
            all_cancels.extend(lease.staged_cancellations)
            if lease.staged_budget is not None:
                all_budgets.append((lease.owner, lease.staged_budget))

        return CallbackChanges(
            staged_state=tuple(all_state_updates),
            staged_events=tuple(all_events),
            staged_continuations=tuple(all_conts),
            staged_cancellations=tuple(all_cancels),
            staged_budgets=tuple(all_budgets),
            errors=tuple(all_errors),
        )

    def stop_background_processes(self) -> None:
        """Owner shutdown: supervised background processes end with the kernel that started them.

        A *host* swap is not an owner shutdown: a background process started by one generation keeps
        running across a reload, which is exactly why the caller persists its identity. Best effort:
        a host that is already gone has nothing left to stop.
        """
        try:
            self._send_request({"protocol_version": 1, "id": self._next_id("bgstop"), "method": "stop_background", "params": {}})
        except Exception:
            pass

    def close(self) -> None:
        """Close connection to host and reap child process group cleanly."""
        self._release_artifacts()
        with self._lifecycle_lock:
            if not self._closed:
                self._closed = True
                self._shutdown = True
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
            if is_stopped and self._proc is not None and self._proc.stdin is not None:
                with self._write_lock:
                    try:
                        self._proc.stdin.close()
                    except OSError:
                        # A dead host cannot consume buffered protocol bytes; still close our pipe.
                        pass

    def __enter__(self) -> HostClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
