"""Direct in-process plugin services: the PluginServices protocol over core state.

One `RuntimeServices` instance is bound to one lease (owner + session + stance) for the
duration of a single plugin callback. Artifact spills publish immutable byte streams;
process management delegates to the core-owned `ProcessRegistry`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import CancelledError
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from voidcode.sdk.history import PAGE_BYTES, HistoryPage
from voidcode.sdk.hooks import FactPage
from voidcode.sdk.json_values import own_json_object
from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.services import (
    ArtifactPage,
    ArtifactReference,
    BackgroundActionStatus,
    CancelSubtreeReceipt,
    ChildOutcome,
    ChildSendReceipt,
    DescendantNode,
    EventView,
    PluginBudget,
    ProcessHandle,
    ProcessOutput,
    ProcessResult,
    SessionInfo,
    SessionUsageReport,
    ToolInvocationReceipt,
)
from voidcode.sdk.transcript import ContextHistory, TranscriptPage

from .events import CHILD_MESSAGE_OWNER, EventType, child_completion_notice_id
from .history_renderer import render_history_page

if TYPE_CHECKING:
    from voidcode.sdk.resources import ResourcePage, ResourceReadRequest
    from voidcode.sdk.tools import ToolDefinition

    from .plugins import LeaseContext, PluginRuntime


class RuntimeServices:
    """`PluginServices` bound directly to one lease, calling the runtime directly."""

    def __init__(self, runtime: PluginRuntime, lease: LeaseContext) -> None:
        self._runtime = runtime
        self._lease = lease

    @property
    def owner(self) -> str:
        return self._active_lease().owner

    def _active_lease(self) -> LeaseContext:
        if self._lease.revoked or self._runtime.closed:
            raise PermissionError("Service lease was revoked")
        if self._lease.hook:
            # Checked per method below; this is the shared hook gate.
            pass
        return self._lease

    def _check_hook(self, method: str) -> None:
        if self._lease.hook and method not in RuntimeServices._HOOK_METHODS:
            raise PermissionError(f"hook leases cannot call {method}: hooks observe, they do not stage state or start work")

    _HOOK_METHODS: frozenset[str] = frozenset(
        {
            "get_state",
            "read_events",
            "read_facts",
            "read_transcript",
            "read_context_history",
            "read_child_history",
            "read_session_history",
            "list_descendants",
            "session_info",
            "get_usage",
            "get_budget",
            "read_artifact",
            "tool_definition",
        }
    )
    # ---- processes (core-owned registry, stance-gated here) ----

    def _require_writable_stance(self, action: str) -> None:
        """Starting or stopping a process is machine work: a restrictive lease may not do it."""
        lease = self._active_lease()
        if lease.hook or lease.readonly:
            raise PermissionError(f"this lease may not {action}: read-only and hook leases do not touch the machine")

    def _stage_observations(self, lease: LeaseContext, effects: ObservedEffects | None) -> None:
        """Append confined changes to the lease; the engine merges them into result.changes.

        Scratch-only changes (sandbox tmpfs, no host path) are skipped: they name sandbox
        paths, not host facts. None (unconfined/absent path) stages nothing.
        """
        if effects is None:
            return
        for change in effects.changes:
            if change.scratch:
                continue
            lease.staged_observations.append(change)

    def _spool_paths(
        self,
        identifier: str,
        stdout_path: Path | str | None,
        stderr_path: Path | str | None,
        spool: Path,
        *,
        service: bool,
    ) -> tuple[Path, Path]:
        """Resolve log targets: runtime-generated spool under confinement, caller paths otherwise.

        Under an active backend, custom stdout/stderr paths are REFUSED with a clear sandbox
        reason (a crafted target would become a parent-opened bypass FD); only
        runtime-generated spool files. Each generated path is resolved and checked before
        mkdir/open. Absent backend preserves today's custom-path behavior.
        """
        if self._runtime.sandbox_backend is not None:
            if stdout_path is not None or stderr_path is not None:
                raise ValueError(
                    "sandbox refuses custom log paths: only runtime-generated spool files "
                    f"(got stdout_path={stdout_path!r} stderr_path={stderr_path!r})"
                )
            root = self._runtime.process_spool_dir or spool
            root = Path(root)
            if not root.is_absolute():
                raise ValueError(f"sandbox process spool root must be absolute: {root}")
            stdout_resolved = (root / f"{identifier}.stdout.log").resolve()
            stderr_resolved = (root / f"{identifier}.stderr.log").resolve()
            # Exact-FD exceptions: the two validated log FDs are pre-opened as the child's
            # stdout/stderr; the paths must stay under the spool ancestor after resolution.
            for resolved in (stdout_resolved, stderr_resolved):
                if resolved != root and root not in resolved.parents:
                    raise ValueError(f"sandbox process spool path escapes its root: {resolved}")
            return stdout_resolved, stderr_resolved
        _ = service
        return (
            Path(stdout_path) if stdout_path is not None else spool / f"{identifier}.stdout.log",
            Path(stderr_path) if stderr_path is not None else spool / f"{identifier}.stderr.log",
        )

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        lease = self._active_lease()
        if lease.hook or lease.readonly:
            raise PermissionError("this lease may not run processes: read-only and hook leases do not touch the machine")
        resolved_cwd = self._runtime.workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"run_process cwd is not a directory: {resolved_cwd}")
        result = self._runtime.processes.run(
            argv,
            cwd=resolved_cwd,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancel_event=self._runtime.cancel_event_for(lease),
            on_output=on_output,
            sandbox=self._runtime.sandbox_backend,
            workspace=self._runtime.workspace,
        )
        self._stage_observations(lease, result.effects)
        return result

    def run_query_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        lease = self._active_lease()
        self._check_hook("run_query_process")
        resolved_cwd = self._runtime.workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"run_query_process cwd is not a directory: {resolved_cwd}")
        sandbox = self._runtime.sandbox_backend
        if sandbox is not None and (sandbox.policy.mode != "read-only" or sandbox.policy.writable_roots):
            sandbox = replace(sandbox, policy=replace(sandbox.policy, mode="read-only", writable_roots=()))
        return self._runtime.processes.run(
            argv,
            cwd=resolved_cwd,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancel_event=self._runtime.cancel_event_for(lease),
            on_output=on_output,
            sandbox=sandbox,
            workspace=self._runtime.workspace,
        )

    def spawn_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bool = False,
        stdout_path: Path | str | None = None,
        stderr_path: Path | str | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        self._require_writable_stance("run background processes")
        lease = self._active_lease()
        identifier = process_id or f"proc-{uuid4().hex[:16]}"
        spool = self._runtime.background_spool
        resolved_cwd = self._runtime.workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"spawn_process cwd is not a directory: {resolved_cwd}")
        stdout_resolved, stderr_resolved = self._spool_paths(identifier, stdout_path, stderr_path, spool, service=False)
        return self._runtime.processes.spawn(
            argv,
            session_id=lease.session_id,
            cwd=resolved_cwd,
            env=env,
            stdin=bool(stdin),
            stdout_path=stdout_resolved,
            stderr_path=stderr_resolved,
            process_id=identifier,
            sandbox=self._runtime.sandbox_backend,
            workspace=self._runtime.workspace,
        )

    def spawn_service_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        self._require_writable_stance("start service processes")
        lease = self._active_lease()
        identifier = process_id or f"svc-{uuid4().hex[:16]}"
        spool = self._runtime.background_spool
        resolved_cwd = self._runtime.workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"spawn_service_process cwd is not a directory: {resolved_cwd}")
        stdout_resolved, stderr_resolved = self._spool_paths(identifier, None, None, spool, service=True)
        handle = self._runtime.processes.spawn(
            argv,
            session_id=lease.session_id,
            cwd=resolved_cwd,
            env=env,
            stdin=True,
            stdout_path=stdout_resolved,
            stderr_path=stderr_resolved,
            process_id=identifier,
            service=True,
            sandbox=self._runtime.sandbox_backend,
            workspace=self._runtime.workspace,
        )
        self._mark_active_process(handle.process_id)
        return handle

    def write_process(self, process_id: str, data: bytes | str) -> int:
        self._require_writable_stance("write to background processes")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        # Bind before the potentially blocking pipe write: a reused server that stops reading
        # stdin must already be cancellable by this invocation, or the cancellation cannot reach it.
        self._mark_active_process(str(process_id))
        return self._runtime.processes.write(str(process_id), payload, session_id=self._active_lease().session_id)

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        # Binding precedes the read: a second invocation reusing a service is already
        # cancellable while it waits for that server's next bytes.
        self._mark_active_process(str(process_id))
        lease = self._active_lease()
        data, total, eof, running = self._runtime.processes.read_output(
            str(process_id),
            stream=stream,
            offset=int(offset),
            limit=int(limit),
            session_id=lease.session_id,
        )
        # User jobs stage their current cumulative effects; service reads never stage
        # (long-lived helper noise is not tool-observed change).
        if self._runtime.processes.service_entry(str(process_id), session_id=lease.session_id) is None:
            self._stage_observations(lease, self._runtime.processes.process_effects(str(process_id), session_id=lease.session_id))
        return ProcessOutput(
            process_id=str(process_id),
            stream=stream,
            offset=int(offset),
            total_bytes=total,
            data=data,
            eof=eof,
            running=running,
        )

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        self._require_writable_stance("stop background processes")
        del reason  # the stop is by process group; a reason would only be a log line we do not own
        lease = self._active_lease()
        gone = self._runtime.processes.cancel(str(process_id), session_id=lease.session_id)
        # Stopping a user job stages its final cumulative effects (the committed prefix).
        # Service stops never stage.
        if self._runtime.processes.service_entry(str(process_id), session_id=lease.session_id) is None:
            self._stage_observations(lease, self._runtime.processes.process_effects(str(process_id), session_id=lease.session_id))
        return gone

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        return self._runtime.processes.list_background(session_id=self._active_lease().session_id)

    def _mark_active_process(self, process_id: str) -> None:
        """Bind one live *service* id to this invocation so cancellation stops its tree.

        Service spawn/read/write call this automatically; ordinary background jobs are never
        bound, so cancelling a log read cannot newly kill a user job.
        """
        lease = self._active_lease()
        if self._runtime.processes.service_entry(str(process_id), session_id=lease.session_id) is None:
            return
        self._runtime.claim_service(lease, str(process_id))

    # ---- artifacts (immutable, spill before the referring fact commits) ----

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        if not isinstance(data, (bytes, str)):
            raise TypeError("artifact data must be bytes or text")
        payload = data.encode("utf-8") if isinstance(data, str) else data
        return self.spill_artifact_stream((payload,), media_type=media_type)

    def spill_artifact_stream(self, chunks: Iterable[bytes], *, media_type: str = "application/octet-stream") -> ArtifactReference:
        from .artifacts import normalize_media_type

        lease = self._active_lease()
        if lease.hook:
            raise PermissionError("hook leases cannot spill artifacts: hooks observe, they produce no result")
        store = self._runtime.require_artifacts()
        spill_id, spill = store.start_spill(normalize_media_type(media_type), owner=lease.token)
        try:
            for chunk in chunks:
                self._active_lease()
                if self._runtime.cancel_event_for(lease).is_set() or (lease.abort_signal is not None and lease.abort_signal.cancelled):
                    raise CancelledError("artifact spill cancelled")
                if not isinstance(chunk, bytes):
                    raise TypeError("artifact stream chunks must be bytes")
                spill.append(chunk)
            self._active_lease()
            if self._runtime.cancel_event_for(lease).is_set() or (lease.abort_signal is not None and lease.abort_signal.cancelled):
                raise CancelledError("artifact spill cancelled")
            digest, size, resolved = store.finish(spill_id)
            self._active_lease()
        except BaseException:
            store.discard(spill_id)
            raise
        return ArtifactReference(sha256=digest, bytes=size, media_type=resolved)

    def read_artifact(
        self,
        sha256: str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        from .artifacts import SHA256_PATTERN, ArtifactNotFoundError

        self._active_lease()
        if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
            raise ValueError("artifact reference must be a lowercase sha256 digest")
        try:
            data, total, eof, media_type = self._runtime.require_artifacts().read(sha256, offset=offset, limit=limit)
        except ArtifactNotFoundError as exc:
            raise FileNotFoundError(f"artifact is not stored: {sha256}") from exc
        return ArtifactPage(
            artifact=ArtifactReference(sha256, total, media_type),
            offset=offset,
            data=data,
            eof=eof,
        )

    # ---- state / events / continuations / budgets (staged on the lease) ----

    def _scope_key(self, scope: str) -> str:
        if scope not in ("session", "workspace"):
            raise ValueError(f"Invalid scope: {scope!r}")
        lease = self._active_lease()
        return lease.session_id if scope == "session" else str(lease.workspace)

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        lease = self._active_lease()
        key = self._scope_key(scope)
        staged = lease.staged_state.get((scope, key))
        if staged is not None:
            st, ver, _baseline = staged
            return (own_json_object(st) if isinstance(st, Mapping) else None, ver)
        st, ver = self._runtime.require_store().plugin_state(lease.owner, scope, key)
        return (own_json_object(st) if isinstance(st, Mapping) else None, ver)

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        lease = self._active_lease()
        self._check_hook("replace_state")
        key = self._scope_key(scope)
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot modify state from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            owned = own_json_object(state)
            if type(expected_version) is not int:
                raise ValueError("expected_version must be an integer")
            staged = lease.staged_state.get((scope, key))
            if staged is not None:
                # Exactly one future version per lease: the first write published
                # it, later writes in the same lease must target that version.
                _st, local_version, baseline = staged
                if expected_version != local_version:
                    raise RuntimeError(f"state CAS conflict within lease: expected version {local_version}, got {expected_version}")
            else:
                _, current = self._runtime.require_store().plugin_state(lease.owner, scope, key)
                if expected_version != current:
                    raise RuntimeError(f"state CAS conflict: expected version {expected_version}, got {current}")
                baseline = current
            local_version = baseline + 1
            lease.staged_state[(scope, key)] = (owned, local_version, baseline)
        return local_version

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        from .storage.store import EventDraft

        lease = self._active_lease()
        self._check_hook("stage_event")
        owned = own_json_object(payload)
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot stage events from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staged_events.append(
                EventDraft(
                    event_type=f"plugin.{lease.owner}.{event_type}",
                    source=f"plugin.{lease.owner}",
                    actor="plugin",
                    payload=owned,
                )
            )

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        from .storage.store import StagedContinuationDraft

        lease = self._active_lease()
        self._check_hook("enqueue_continuation")
        owned = own_json_object(payload)
        if role not in ("system", "user"):
            raise ValueError("role must be 'system' or 'user'")
        if lease.hook:
            raise PermissionError("post-tool hooks cannot start nested governed work")
        key = str(idempotency_key) if idempotency_key is not None else None
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot enqueue continuations from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            existing = next(
                (c.continuation_id for c in lease.staged_continuations if c.idempotency_key == key and key is not None),
                None,
            )
            if existing is None and key is not None:
                existing = self._runtime.require_store().continuation_id_for_idempotency(lease.session_id, lease.owner, key)
            if existing is not None:
                return existing
            # A keyed identity is derived from the key itself, so two
            # concurrent leases that both stage before either commits
            # return the SAME id: the store's key dedupe then keeps
            # exactly one durable row that every caller's id names.
            cid = uuid4().hex if key is None else hashlib.sha256("\0".join((lease.session_id, lease.owner, key)).encode("utf-8")).hexdigest()
            lease.op_counter += 1
            lease.staged_continuations.append(
                StagedContinuationDraft(
                    continuation_id=cid,
                    owner=lease.owner,
                    source=f"plugin.{lease.owner}",
                    payload=owned,
                    idempotency_key=key,
                    ordinal=lease.op_counter,
                    role=role,
                )
            )
        return cid

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        from .storage.store import StagedCancellation

        lease = self._active_lease()
        self._check_hook("cancel_continuations")
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot cancel continuations from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.op_counter += 1
            lease.staged_cancellations.append(
                StagedCancellation(
                    owner=lease.owner,
                    continuation_id=str(continuation_id) if continuation_id is not None else None,
                    ordinal=lease.op_counter,
                )
            )

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        idempotency_key: str | None = None,
    ) -> str:
        lease = self._active_lease()
        self._check_hook("spawn_child")
        # A read-only owner may delegate read-only work (reviews under plan mode):
        # the child inherits the restrictive stance, so no privilege is created.
        if lease.readonly and not readonly:
            raise PermissionError("a read-only lease may only spawn child work that stays read-only")
        if tool_allowlist is not None and (
            not isinstance(tool_allowlist, (list, tuple)) or not all(isinstance(item, str) for item in tool_allowlist)
        ):
            raise ValueError("tool_allowlist must be a list of tool names or null")
        if max_steps is not None and type(max_steps) is not int:
            raise ValueError("max_steps must be an integer or null")
        if not isinstance(lease.root_action_id, str) or not lease.root_action_id:
            raise ValueError("child spawning requires a captured root action identity")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("idempotency_key must be a non-empty string or null")
        callback = self._runtime.spawn_child_callback
        if callback is None:
            raise RuntimeError("child spawning is unavailable")
        return callback(
            lease.session_id,
            str(prompt),
            str(label),
            max_steps,
            None if tool_allowlist is None else tuple(str(item) for item in tool_allowlist),
            bool(readonly) or lease.readonly,
            bool(notify_on_completion),
            lease.owner,
            idempotency_key,
            lease.root_action_id,
            lease.initial_root_budget,
            self._runtime.admission_guard(lease),
        )

    def find_child(self, *, idempotency_key: str) -> str | None:
        lease = self._active_lease()
        self._check_hook("find_child")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        callback = self._runtime.find_child_callback
        if callback is None:
            raise RuntimeError("child lookup is unavailable")
        found = callback(lease.session_id, lease.owner, idempotency_key)
        return None if found is None else str(found)

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        from voidcode.sdk.services import ChildSendReceipt as Receipt

        lease = self._active_lease()
        self._check_hook("send_child")
        if not isinstance(child_session_id, str) or not child_session_id:
            raise ValueError("child_session_id must be a non-empty string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("child message prompt must be a non-empty string")
        # A parent that is now read-only may not steer a child that is still writable:
        # the child may have been spawned before plan mode/read-only intent existed.
        if lease.readonly:
            store = self._runtime.require_store()
            child_record = store.session(child_session_id)
            if child_record.parent_session_id != lease.session_id:
                raise PermissionError("child session does not belong to this parent session")
            if "read-only" not in store.session_overlays(child_session_id):
                raise PermissionError("a read-only parent cannot steer a child that is not durably read-only")
        callback = self._runtime.send_child_callback
        if callback is None:
            raise RuntimeError("child messaging is unavailable")
        payload = callback(lease.session_id, child_session_id, prompt, str(label), bool(notify_on_completion), self._runtime.admission_guard(lease))
        if not isinstance(payload, Mapping):
            raise RuntimeError("runtime returned a malformed child send receipt")
        delivery = str(payload.get("delivery", "queued"))
        if delivery not in ("started", "queued"):
            raise RuntimeError(f"runtime returned an unknown child delivery: {delivery!r}")
        return Receipt(
            child_session_id=str(payload.get("child_session_id", child_session_id)),
            delivery=delivery,  # type: ignore[arg-type]
            child_status=str(payload.get("child_status", "unknown")),
            run_id=None if payload.get("run_id") is None else str(payload["run_id"]),
            message_id=None if payload.get("message_id") is None else str(payload["message_id"]),
        )

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        from .storage.store import StagedCancellation

        lease = self._active_lease()
        self._check_hook("acknowledge_child_completion")
        if not isinstance(child_session_id, str) or not child_session_id:
            raise ValueError("child_session_id must be a non-empty string")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        child_record = self._runtime.require_store().session(child_session_id)
        if child_record.parent_session_id != lease.session_id:
            raise PermissionError("child session does not belong to this parent session")
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot acknowledge child completions from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.op_counter += 1
            lease.staged_cancellations.append(
                StagedCancellation(
                    owner=CHILD_MESSAGE_OWNER,
                    continuation_id=child_completion_notice_id(lease.session_id, child_session_id, run_id),
                    ordinal=lease.op_counter,
                )
            )

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        from voidcode.sdk.services import ChildOutcome as Outcome

        lease = self._active_lease()
        self._check_hook("join_child")
        callback = self._runtime.join_child_callback
        if callback is None:
            raise RuntimeError("child joining is unavailable")
        timeout = float(timeout_seconds) if isinstance(timeout_seconds, (int, float)) and not isinstance(timeout_seconds, bool) else 300.0
        payload = callback(lease.session_id, str(child_session_id), timeout)
        if not isinstance(payload, Mapping):
            raise RuntimeError("runtime returned a malformed child outcome")
        raw_status = payload.get("status", "failed")
        status: Literal["completed", "failed", "interrupted", "cancelled", "running", "waiting"]
        if raw_status in ("completed", "failed", "interrupted", "cancelled", "running", "waiting"):
            status = raw_status  # ty: ignore[invalid-assignment]
        else:
            raise RuntimeError(f"runtime returned an unknown child status: {raw_status!r}")
        history_bytes = payload.get("history_bytes")
        output_bytes = payload.get("output_bytes")
        return Outcome(
            child_session_id=str(payload.get("child_session_id", child_session_id)),
            status=status,
            output=None if payload.get("output") is None else str(payload["output"]),
            error=None if payload.get("error") is None else str(payload["error"]),
            stop_cause=None if payload.get("stop_cause") is None else str(payload["stop_cause"]),
            run_id=None if payload.get("run_id") is None else str(payload["run_id"]),
            history_sha256=None if payload.get("history_sha256") is None else str(payload["history_sha256"]),
            history_bytes=history_bytes if isinstance(history_bytes, int) and not isinstance(history_bytes, bool) else None,
            history_truncated=bool(payload.get("history_truncated", False)),
            output_preview=None if payload.get("output_preview") is None else str(payload["output_preview"]),
            output_sha256=None if payload.get("output_sha256") is None else str(payload["output_sha256"]),
            output_bytes=output_bytes if isinstance(output_bytes, int) and not isinstance(output_bytes, bool) else None,
        )

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        lease = self._active_lease()
        self._check_hook("cancel_child")
        callback = self._runtime.cancel_child_callback
        if callback is None:
            raise RuntimeError("child cancellation is unavailable")
        return callback(lease.session_id, str(child_session_id), str(reason))

    def list_descendants(self) -> tuple[DescendantNode, ...]:
        lease = self._active_lease()
        callback = self._runtime.list_descendants_callback
        if callback is None:
            raise RuntimeError("descendant listing is unavailable")
        rows = callback(lease.session_id)
        if not isinstance(rows, tuple) or not all(isinstance(row, DescendantNode) for row in rows):
            raise RuntimeError("runtime returned a malformed descendant listing")
        return rows

    def cancel_descendant_subtree(
        self,
        target_session_id: str,
        *,
        expected_run_id: str | None,
        reason: str = "owner cancellation",
    ) -> CancelSubtreeReceipt:
        lease = self._active_lease()
        self._check_hook("cancel_descendant_subtree")
        if not isinstance(target_session_id, str) or not target_session_id:
            raise ValueError("target_session_id must be a non-empty string")
        if expected_run_id is not None and (not isinstance(expected_run_id, str) or not expected_run_id):
            raise ValueError("expected_run_id must be a non-empty string or null")
        if lease.internal_restricted:
            raise PermissionError("Cannot cancel a descendant subtree from a read-only service lease")
        callback = self._runtime.cancel_subtree_callback
        if callback is None:
            raise RuntimeError("subtree cancellation is unavailable")
        receipt = callback(lease.session_id, target_session_id, expected_run_id, str(reason))
        if not isinstance(receipt, CancelSubtreeReceipt):
            raise RuntimeError("runtime returned a malformed subtree receipt")
        return receipt

    def enqueue_background_action(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        from .storage.store import BACKGROUND_ACTION_SOURCE, StagedContinuationDraft

        lease = self._active_lease()
        self._check_hook("enqueue_background_action")
        owned = own_json_object(payload)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError("idempotency_key must be a non-empty string or null")
        key = str(idempotency_key) if idempotency_key is not None else None
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot enqueue background actions from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            existing = next(
                (c.continuation_id for c in lease.staged_continuations if c.idempotency_key == key and key is not None),
                None,
            )
            if existing is None and key is not None:
                existing = self._runtime.require_store().background_action_id_for_idempotency(lease.session_id, lease.owner, key)
            if existing is not None:
                return existing
            if not isinstance(lease.root_action_id, str) or not lease.root_action_id:
                raise RuntimeError("background action requires a captured root action identity")
            # Same deterministic identity rule as continuations: two concurrent leases that
            # both stage before either commits return the SAME id, and the store keeps one row.
            cid = uuid4().hex if key is None else hashlib.sha256("\0".join((lease.session_id, lease.owner, key)).encode("utf-8")).hexdigest()
            lease.op_counter += 1
            authority = None
            if lease.origin_visible_tools is not None and lease.origin_readonly is not None:
                authority = {
                    "visible_tools": list(lease.origin_visible_tools),
                    "readonly": lease.origin_readonly,
                    "revision_id": lease.revision_id,
                }
            lease.staged_continuations.append(
                StagedContinuationDraft(
                    continuation_id=cid,
                    owner=lease.owner,
                    source=BACKGROUND_ACTION_SOURCE,
                    payload={
                        "action": owned,
                        "root_action_budget": lease.initial_root_budget,
                        "tool_wait_authority": authority,
                    },
                    idempotency_key=key,
                    ordinal=lease.op_counter,
                    root_id=lease.root_action_id,
                    role="system",
                )
            )
        self._runtime.notify_background_action()
        return cid

    def cancel_background_action(self, action_id: str, reason: str = "owner cancellation") -> bool:
        lease = self._active_lease()
        self._check_hook("cancel_background_action")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("action_id must be a non-empty string")
        callback = self._runtime.cancel_background_action_callback
        if callback is None:
            raise RuntimeError("background action cancellation is unavailable")
        return callback(lease.session_id, lease.owner, action_id, str(reason))

    def background_action_status(self, action_id: str) -> BackgroundActionStatus | None:
        lease = self._active_lease()
        self._check_hook("background_action_status")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("action_id must be a non-empty string")
        record = self._runtime.require_store().background_action(action_id)
        if record is None or record.owner != lease.owner or record.session_id != lease.session_id:
            return None
        return self._runtime.background_action_status(record)

    def project_window(self, *, covered_from_sequence: int, covered_to_sequence: int, replacement: str) -> None:
        lease = self._active_lease()
        self._check_hook("project_window")
        if lease.internal_restricted:
            raise PermissionError("Cannot project a session window from a read-only service lease")
        if type(covered_from_sequence) is not int or type(covered_to_sequence) is not int:
            raise ValueError("projection boundaries must be integers")
        self._runtime.store_window_projection(lease, covered_from_sequence, covered_to_sequence, str(replacement))

    def window_projection(self) -> Mapping[str, object] | None:
        lease = self._active_lease()
        for event in reversed(lease.staged_events):
            if event.event_type == EventType.WINDOW_PROJECTED:
                return dict(event.payload)
        record = self._runtime.require_store().window_projection(lease.session_id)
        return None if record is None else asdict(record)

    def set_overlay(self, tier: Literal["read-only"] = "read-only") -> None:
        lease = self._active_lease()
        self._check_hook("set_overlay")
        self._runtime.require_store().set_session_overlay(lease.session_id, lease.owner, str(tier))

    def clear_overlay(self) -> bool:
        lease = self._active_lease()
        self._check_hook("clear_overlay")
        return self._runtime.require_store().clear_session_overlay(lease.session_id, lease.owner)

    def schedule_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        lease = self._active_lease()
        self._check_hook("schedule_wakeup")
        if lease.internal_restricted:
            raise PermissionError("Cannot schedule wakeups from a read-only service lease")
        if type(due_at_ms) is not int or due_at_ms < 0:
            raise ValueError("due_at_ms must be >= 0")
        if role not in ("system", "user"):
            raise ValueError("role must be 'system' or 'user'")
        key = str(idempotency_key) if isinstance(idempotency_key, str) and idempotency_key else None
        if key is None:
            wakeup_id = uuid4().hex
        else:
            wakeup_id = hashlib.sha256("\0".join((lease.session_id, lease.owner, "wakeup", key)).encode("utf-8")).hexdigest()
        if not isinstance(lease.root_action_id, str) or not lease.root_action_id:
            raise RuntimeError("wakeup scheduling requires a captured root action identity")
        self._runtime.require_store().schedule_wakeup(
            wakeup_id,
            lease.owner,
            str(lease.workspace),
            lease.session_id,
            own_json_object(payload),
            due_at_ms,
            str(role),
            root_action_id=lease.root_action_id,
            root_action_budget=lease.initial_root_budget,
        )
        return wakeup_id

    def cancel_wakeup(self, wakeup_id: str) -> bool:
        lease = self._active_lease()
        self._check_hook("cancel_wakeup")
        if lease.internal_restricted:
            raise PermissionError("Cannot cancel wakeups from a read-only service lease")
        return self._runtime.require_store().cancel_wakeup(str(wakeup_id), lease.owner)

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        lease = self._active_lease()
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("after_sequence must be >= 0 and limit within 1..200")
        return self._runtime.fact_page(lease.session_id, after_sequence, limit)

    def read_context_history(self, *, limit: int = 600) -> ContextHistory:
        lease = self._active_lease()
        if type(limit) is not int or not 1 <= limit <= 2000:
            raise ValueError("limit must be within 1..2000")
        return self._runtime.context_history(lease.session_id, limit)

    def read_transcript(self, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        lease = self._active_lease()
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("after_sequence must be >= 0 and limit within 1..200")
        return self._runtime.transcript_page(lease.session_id, after_sequence, limit)

    def read_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        lease = self._active_lease()
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or limit < 1 or limit > 200:
            raise ValueError("after_sequence must be >= 0 and limit within 1..200")
        if type(include_payload) is not bool:
            raise ValueError("include_payload must be a boolean")
        return self._runtime.event_views(lease.session_id, after_sequence, limit, include_payload)

    def session_info(self) -> SessionInfo:
        lease = self._active_lease()
        return self._runtime.require_store().session_info(lease.session_id, lease.owner)

    def read_child_history(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> HistoryPage:
        lease = self._active_lease()
        if not isinstance(child_session_id, str) or not child_session_id or child_session_id == lease.session_id:
            raise ValueError("child_session_id must name another session")
        return self.read_session_history(child_session_id, after_sequence=after_sequence, offset=offset, limit=limit)

    def read_session_history(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
        page_bytes: int = PAGE_BYTES,
    ) -> HistoryPage:
        lease = self._active_lease()
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        if type(after_sequence) is not int or after_sequence < 0 or type(offset) is not int or offset < 0:
            raise ValueError("after_sequence and offset must be non-negative integers")
        if type(page_bytes) is not int or page_bytes < 1 or page_bytes > PAGE_BYTES:
            raise ValueError(f"page_bytes must be an integer within 1..{PAGE_BYTES}")
        limit = max(1, min(limit, 200))
        store = self._runtime.require_store()
        if session_id != lease.session_id:
            caller = store.session(lease.session_id)
            target = store.session(session_id)
            if target.workspace != caller.workspace or not self._runtime.is_descendant(target, caller.session_id):
                raise PermissionError("session history is readable only for this session or its own descendants")
        events = store.events(session_id, after_sequence=after_sequence, limit=limit + 1)
        page = render_history_page(events[:limit], after_sequence=after_sequence, offset=offset, page_bytes=page_bytes)
        return replace(page, truncated=page.truncated or len(events) > limit)

    def tool_definition(self, name: str) -> ToolDefinition | None:
        """One definition from the active revision's dispatch snapshot, with this session's visibility.

        Answered by the core, never from a runtime-local copy: only the core knows the reserved
        core surface and the session's profile scope and read-only stance. ``None`` covers both an
        unknown name and a name this session may not see.
        """
        from voidcode.sdk.tools import ToolDefinition as Definition

        lease = self._active_lease()
        callback = self._runtime.tool_definition_callback
        if callback is None:
            raise RuntimeError("tool definitions are unavailable")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool_definition requires a non-empty name")
        definition, _reason = callback(lease.session_id, name.strip(), lease.revision_id)
        if definition is None:
            return None
        if isinstance(definition, Definition):
            return definition
        if not isinstance(definition, Mapping):
            raise RuntimeError("tool definitions are unavailable")
        from voidcode.sdk.codec import decode_tool_definition

        return decode_tool_definition(definition)

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None:
        lease = self._active_lease()
        return self._runtime.execute_query(str(name), own_json_object(dict(arguments or {})), session_id=lease.session_id, workspace=lease.workspace)

    def read_resource(self, request: ResourceReadRequest) -> ResourcePage | None:
        from voidcode.sdk.invocation import ToolContext

        lease = self._active_lease()
        self._check_hook("read_resource")
        abort_signal = self._abort_signal_for(lease)
        context = ToolContext(
            workspace=lease.workspace,
            session_id=lease.session_id,
            run_id=lease.run_id,
            invocation_id=lease.invocation_id,
            revision_id=lease.revision_id,
            actor=lease.actor,
            invocation_depth=lease.invocation_depth,
            abort_signal=abort_signal,
        )
        return self._runtime.read_resource(request, context=context)

    def _abort_signal_for(self, lease: LeaseContext):  # type: ignore[no-untyped-def]
        """The caller's cancellation handle, so reader paging can stop promptly."""
        event = self._runtime.cancel_event_for(lease)
        if event.is_set():
            return _CancelledAbortSignal()
        return _LeaseAbortSignal(event)

    def invoke_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        expected_revision: str,
    ) -> ToolInvocationReceipt:
        from voidcode.sdk.codec import encode_tool_call
        from voidcode.sdk.tools import ToolCall

        from .storage.store import EventDraft

        lease = self._active_lease()
        self._check_hook("invoke_tool")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("invoke_tool requires a non-empty name")
        if expected_revision != self._runtime.revision_id:
            raise ValueError("invoke_tool expected_revision differs from the active revision")
        if not isinstance(arguments, Mapping) and arguments is not None:
            raise ValueError("invoke_tool arguments must be an object")
        if lease.background_action_id is not None and (
            lease.origin_visible_tools is None or lease.origin_readonly is None or not lease.root_action_id
        ):
            raise PermissionError("background tool requests require captured origin tool, stance, and root authorization")
        request_id = uuid4().hex
        depth = lease.invocation_depth + 1
        call = ToolCall(name, own_json_object(dict(arguments or {})), tool_call_id=request_id)
        root_id = lease.root_action_id or ""
        payload: dict[str, object] = {
            "request_id": request_id,
            "call": encode_tool_call(call),
            "expected_revision": expected_revision,
            "requester": lease.owner,
            "root_id": root_id,
            "root_action_budget": lease.initial_root_budget,
            "caller_invocation_id": lease.invocation_id,
            "depth": depth,
        }
        if lease.origin_visible_tools is not None:
            payload["origin_visible_tools"] = list(lease.origin_visible_tools)
        if lease.origin_readonly is not None:
            payload["origin_readonly"] = lease.origin_readonly
        if lease.background_action_id is not None:
            payload["background_action_id"] = lease.background_action_id
            payload["result_delivery"] = "requester"
        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staged_events.append(EventDraft(EventType.TOOL_DEFERRED, "core", "plugin", payload))
        return ToolInvocationReceipt(request_id=request_id, tool_name=name, depth=depth)

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None, include_context: bool = False) -> str:
        lease = self._active_lease()
        self._check_hook("model_query")
        callback = self._runtime.model_query_callback
        if callback is None:
            raise RuntimeError("governed model queries are unavailable")
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or null")
        if max_output_tokens is not None and type(max_output_tokens) is not int:
            raise ValueError("max_output_tokens must be an integer or null")
        if type(include_context) is not bool:
            raise ValueError("include_context must be a boolean")
        return callback(lease.session_id, str(prompt), model, max_output_tokens, include_context)

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        lease = self._active_lease()
        if type(since_sequence) is not int:
            raise ValueError("since_sequence must be an integer")
        if until_sequence is not None and type(until_sequence) is not int:
            raise ValueError("until_sequence must be an integer or null")
        callback = self._runtime.usage_callback
        if callback is None:
            raise RuntimeError("usage service is unavailable in this runtime")
        return callback(lease.session_id, since_sequence, until_sequence)

    def get_budget(self) -> PluginBudget | None:
        lease = self._active_lease()
        with self._runtime.lease_lock:
            staged_budget = lease.staged_budget
        if staged_budget == "clear":
            return None
        if staged_budget is not None:
            return staged_budget
        callback = self._runtime.budget_callback
        if callback is None:
            raise RuntimeError("budget service is unavailable in this runtime")
        return callback(lease.session_id, lease.owner)

    def set_budget(
        self,
        *,
        since_sequence: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        lease = self._active_lease()
        self._check_hook("set_budget")
        if type(since_sequence) is not int:
            raise ValueError("since_sequence must be an integer")
        budget = PluginBudget(
            owner=lease.owner,
            since_sequence=since_sequence,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
            max_elapsed_seconds=max_elapsed_seconds,
        )
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot set budget from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staged_budget = budget

    def clear_budget(self) -> None:
        lease = self._active_lease()
        self._check_hook("clear_budget")
        with self._runtime.lease_lock:
            if lease.internal_restricted:
                raise PermissionError("Cannot clear budget from a read-only service lease")
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staged_budget = "clear"


class _LeaseAbortSignal:
    """A ``ProviderAbortSignal`` view over one lease's runtime cancel event."""

    def __init__(self, event) -> None:  # type: ignore[no-untyped-def]
        self._event = event

    @property
    def cancelled(self) -> bool:
        return bool(self._event.is_set())

    @property
    def reason(self) -> str | None:
        return "cancelled" if self.cancelled else None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        del reason
        if value:
            self._event.set()
        else:
            self._event.clear()


class _CancelledAbortSignal:
    """An already-cancelled signal for a revoked parent lease."""

    @property
    def cancelled(self) -> bool:
        return True

    @property
    def reason(self) -> str | None:
        return "cancelled"

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        del value, reason


class ResourceReaderServicesView:
    """The ``ResourceReaderServices`` protocol over one reader sub-lease.

    A thin wrapper, not a second implementation: the four read verbs delegate
    to the same ``RuntimeServices`` gates (lease liveness, lineage, visibility),
    and the sub-lease itself is read-only with no internal work. Staging,
    spawn, process, and network methods do not exist here.
    """

    def __init__(self, runtime: PluginRuntime, lease: LeaseContext) -> None:
        self._services = RuntimeServices(runtime, lease)

    @property
    def owner(self) -> str:
        return self._services.owner

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        return self._services.get_state(scope)

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        return self._services.read_artifact(sha256, offset=offset, limit=limit)

    def tool_definition(self, name: str) -> ToolDefinition | None:
        return self._services.tool_definition(name)

    def read_session_history(self, session_id: str, *, after_sequence: int = 0, offset: int = 0, limit: int = 100) -> HistoryPage:
        return self._services.read_session_history(session_id, after_sequence=after_sequence, offset=offset, limit=limit)
