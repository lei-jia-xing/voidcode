"""Direct in-process plugin services: the PluginServices protocol over core state.

One `RuntimeServices` instance is bound to one lease (owner + session + stance) for the
duration of a single plugin callback. Every method below is the direct equivalent of one
former reverse-RPC: same validation, same errors, same staged side effects — with no
transport, no tokens on the wire, and no chunking. Artifact spills write whole payloads;
process management delegates to the core-owned `ProcessRegistry`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from voidcode.sdk.hooks import FactPage
from voidcode.sdk.json_values import own_json_object
from voidcode.sdk.services import (
    CHILD_MESSAGE_OWNER,
    ArtifactPage,
    ArtifactReference,
    ChildOutcome,
    ChildSendReceipt,
    EventView,
    PluginBudget,
    ProcessHandle,
    ProcessOutput,
    ProcessResult,
    SessionInfo,
    SessionUsageReport,
    ToolInvocationReceipt,
    child_completion_notice_id,
)
from voidcode.sdk.transcript import ContextHistory, TranscriptPage

if TYPE_CHECKING:
    from voidcode.sdk.tools import ToolDefinition

    from .plugins import LeaseContext, PluginRuntime


def _artifact_sha256(reference: ArtifactReference | Mapping[str, object] | str) -> str:
    """Accept the reference shapes a plugin legitimately holds and extract its digest."""
    if isinstance(reference, ArtifactReference):
        return reference.sha256
    if isinstance(reference, str):
        return reference
    if isinstance(reference, Mapping):
        value = reference.get("sha256")
        if isinstance(value, str):
            return value
    raise ValueError("artifact reference must carry a sha256 digest")


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
            "get_state_schema_version",
            "read_events",
            "read_facts",
            "read_transcript",
            "read_context_history",
            "read_child_events",
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
        return self._runtime.processes.run(
            argv,
            cwd=resolved_cwd,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancel_event=self._runtime.cancel_event_for(lease),
            on_output=on_output,
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
        return self._runtime.processes.spawn(
            argv,
            session_id=lease.session_id,
            cwd=resolved_cwd,
            env=env,
            stdin=bool(stdin),
            stdout_path=Path(stdout_path) if stdout_path is not None else spool / f"{identifier}.stdout.log",
            stderr_path=Path(stderr_path) if stderr_path is not None else spool / f"{identifier}.stderr.log",
            process_id=identifier,
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
        handle = self._runtime.processes.spawn(
            argv,
            session_id=lease.session_id,
            cwd=resolved_cwd,
            env=env,
            stdin=True,
            stdout_path=spool / f"{identifier}.stdout.log",
            stderr_path=spool / f"{identifier}.stderr.log",
            process_id=identifier,
            service=True,
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
        data, total, eof, running = self._runtime.processes.read_output(
            str(process_id),
            stream=stream,
            offset=int(offset),
            limit=int(limit),
            session_id=self._active_lease().session_id,
        )
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
        return self._runtime.processes.cancel(str(process_id), session_id=self._active_lease().session_id)

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

    # ---- artifacts (core store, whole payloads, no chunking) ----

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        from .artifacts import normalize_media_type

        lease = self._active_lease()
        if lease.hook:
            raise PermissionError("hook leases cannot spill artifacts: hooks observe, they produce no result")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        media_type = str(media_type)
        if not media_type:
            raise ValueError("spill_artifact media_type must not be empty")
        store = self._runtime.require_artifacts()
        spill_id, spill = store.start_spill(normalize_media_type(media_type), owner=lease.token)
        try:
            spill.append(payload)
            digest, size, resolved = store.finish(spill_id)
        except Exception:
            store.discard(spill_id)
            raise
        return ArtifactReference(sha256=digest, bytes=size, media_type=resolved)

    def read_artifact(
        self,
        reference: ArtifactReference | Mapping[str, object] | str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        from .artifacts import SHA256_PATTERN

        digest = _artifact_sha256(reference)
        if not SHA256_PATTERN.match(digest):
            raise ValueError("artifact reference must be a lowercase sha256 digest")
        data, total, eof, media_type = self._runtime.require_artifacts().read(digest, offset=int(offset), limit=int(limit))
        return ArtifactPage(
            sha256=digest,
            offset=int(offset),
            total_bytes=total,
            data=data,
            eof=eof,
            media_type=media_type,
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

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        lease = self._active_lease()
        _payload, _version, stored_schema = self._runtime.require_store().plugin_state_meta(lease.owner, scope, self._scope_key(scope))
        # The stored row's schema version only: 0 means "nothing stored yet", which is
        # unambiguous for an owner deciding whether it must migrate its own state.
        return stored_schema

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
        max_steps: int = 32,
        readonly: bool = False,
        notify_on_completion: bool = False,
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
        if type(max_steps) is not int:
            raise ValueError("max_steps must be an integer")
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
        )

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
        payload = callback(lease.session_id, child_session_id, prompt, str(label), bool(notify_on_completion))
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
            if event.event_type == "kernel.window_projected":
                return dict(event.payload)
        record = self._runtime.require_store().window_projection(lease.session_id)
        return None if record is None else asdict(record)

    def clear_window_projection(self) -> bool:
        lease = self._active_lease()
        self._check_hook("clear_window_projection")
        if lease.internal_restricted:
            raise PermissionError("Cannot clear a session window projection from a read-only service lease")
        return self._runtime.require_store().clear_window_projection(lease.session_id)

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
        self._runtime.require_store().schedule_wakeup(
            wakeup_id,
            lease.owner,
            str(lease.workspace),
            lease.session_id,
            own_json_object(payload),
            due_at_ms,
            str(role),
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

    def read_child_events(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        lease = self._active_lease()
        if not isinstance(child_session_id, str) or not child_session_id or child_session_id == lease.session_id:
            raise ValueError("child_session_id must name another session")
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or limit < 1 or limit > 200:
            raise ValueError("after_sequence must be >= 0 and limit within 1..200")
        if type(include_payload) is not bool:
            raise ValueError("include_payload must be a boolean")
        store = self._runtime.require_store()
        caller = store.session(lease.session_id)
        child = store.session(child_session_id)
        if child.workspace != caller.workspace or not self._runtime.is_descendant(child, caller.session_id):
            raise PermissionError("child events are readable only for this session's own descendants")
        return self._runtime.event_views(child_session_id, after_sequence, limit, include_payload)

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
        request_id = uuid4().hex
        depth = lease.invocation_depth + 1
        call = ToolCall(name, own_json_object(dict(arguments or {})), tool_call_id=request_id)
        store = self._runtime.store
        checkpoint = None if store is None else store.checkpoint(lease.session_id)
        root_id = "" if checkpoint is None or checkpoint.kind == "terminal" else str(checkpoint.payload.get("root_id", ""))
        with self._runtime.lease_lock:
            if lease.revoked:
                raise PermissionError("Service lease was revoked")
            lease.staged_events.append(
                EventDraft(
                    "kernel.tool_deferred",
                    "core",
                    "plugin",
                    {
                        "request_id": request_id,
                        "call": encode_tool_call(call),
                        "expected_revision": expected_revision,
                        "requester": lease.owner,
                        "root_id": root_id,
                        "caller_invocation_id": lease.invocation_id,
                        "depth": depth,
                    },
                )
            )
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
