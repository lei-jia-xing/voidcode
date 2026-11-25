"""Process services: one-shot runs, background jobs, and generation-owned services."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.resources import ResourceProcessStatus
from voidcode.sdk.services import ProcessHandle, ProcessOutput, ProcessResult, ProcessStream

from ._lease import ServiceLease

if TYPE_CHECKING:
    from ..plugins import LeaseContext


class ProcessServices(ServiceLease):
    """Core-owned registry access, stance-gated per lease."""

    def _require_writable_stance(self, action: str) -> None:
        """Starting or stopping a process is machine work: a restrictive lease may not do it."""
        lease = self._active_lease()
        if lease.identity.hook or lease.identity.readonly:
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
            lease.staging.observations.append(change)

    def _spool_paths(
        self,
        identifier: str,
        stdout_path: Path | str | None,
        stderr_path: Path | str | None,
    ) -> tuple[Path, Path]:
        """Resolve log targets: generated paths use the stable host spool; caller paths win when unconfined.

        Under an active backend, custom stdout/stderr paths are REFUSED with a clear sandbox
        reason (a crafted target would become a parent-opened bypass FD); only
        runtime-generated spool files. Each generated path is resolved and checked before
        mkdir/open. Absent backend preserves today's custom-path behavior.
        """
        root = self._runtime.process_host.spool_dir
        if self._runtime.sandbox_backend is not None:
            if stdout_path is not None or stderr_path is not None:
                raise ValueError(
                    "sandbox refuses custom log paths: only runtime-generated spool files "
                    f"(got stdout_path={stdout_path!r} stderr_path={stderr_path!r})"
                )
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
        return (
            Path(stdout_path) if stdout_path is not None else root / f"{identifier}.stdout.log",
            Path(stderr_path) if stderr_path is not None else root / f"{identifier}.stderr.log",
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
        on_output: Callable[[ProcessStream, bytes], None] | None = None,
    ) -> ProcessResult:
        lease = self._active_lease()
        if lease.identity.hook or lease.identity.readonly:
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
        on_output: Callable[[ProcessStream, bytes], None] | None = None,
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
        resolved_cwd = self._runtime.workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"spawn_process cwd is not a directory: {resolved_cwd}")
        stdout_resolved, stderr_resolved = self._spool_paths(
            identifier,
            stdout_path,
            stderr_path,
        )
        return self._runtime.processes.spawn(
            argv,
            owner=lease.identity.owner,
            session_id=lease.identity.session_id,
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
        resolved_cwd = self._runtime.workspace if cwd is None else Path(cwd)
        if cwd is not None and not resolved_cwd.is_dir():
            raise ValueError(f"spawn_service_process cwd is not a directory: {resolved_cwd}")
        stdout_resolved, stderr_resolved = self._spool_paths(
            identifier,
            None,
            None,
        )
        handle = self._runtime.processes.spawn(
            argv,
            owner=lease.identity.owner,
            session_id=lease.identity.session_id,
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
        lease = self._active_lease()
        # Bind before the potentially blocking pipe write: a reused server that stops reading
        # stdin must already be cancellable by this invocation, or the cancellation cannot reach it.
        self._mark_active_process(str(process_id))
        return self._runtime.processes.write(
            str(process_id),
            payload,
            owner=lease.identity.owner,
            session_id=lease.identity.session_id,
            cancel_event=self._runtime.cancel_event_for(lease),
        )

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: ProcessStream = "stdout",
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
            owner=lease.identity.owner,
            session_id=lease.identity.session_id,
        )
        # User jobs stage their current cumulative effects; service reads never stage
        # (long-lived helper noise is not tool-observed change).
        if self._runtime.processes.service_entry(str(process_id), owner=lease.identity.owner, session_id=lease.identity.session_id) is None:
            self._stage_observations(
                lease,
                self._runtime.processes.process_effects(str(process_id), owner=lease.identity.owner, session_id=lease.identity.session_id),
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
        lease = self._active_lease()
        gone = self._runtime.processes.cancel(str(process_id), owner=lease.identity.owner, session_id=lease.identity.session_id)
        # Stopping a user job stages its final cumulative effects (the committed prefix).
        # Service stops never stage.
        if self._runtime.processes.service_entry(str(process_id), owner=lease.identity.owner, session_id=lease.identity.session_id) is None:
            self._stage_observations(
                lease,
                self._runtime.processes.process_effects(str(process_id), owner=lease.identity.owner, session_id=lease.identity.session_id),
            )
        return gone

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        lease = self._active_lease()
        return self._runtime.processes.list_background(owner=lease.identity.owner, session_id=lease.identity.session_id)

    def _list_resource_processes(self) -> tuple[ResourceProcessStatus, ...]:
        """Path-free process status for the current reader owner and caller session."""
        lease = self._active_lease()
        return tuple(
            ResourceProcessStatus(process_id=handle.process_id, running=handle.running, exit_code=handle.exit_code)
            for handle in self._runtime.processes.list_background(owner=lease.identity.owner, session_id=lease.identity.session_id)
        )

    def _read_resource_process_output(
        self,
        process_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        """Read process bytes without the ordinary service-read cancellation/effect side effects."""
        lease = self._active_lease()
        if not isinstance(process_id, str) or not process_id:
            raise ValueError("process_id must be a non-empty string")
        if stream not in ("stdout", "stderr"):
            raise ValueError("stream must be 'stdout' or 'stderr'")
        if type(offset) is not int or offset < 0:
            raise ValueError("process output offset must be a non-negative integer")
        if type(limit) is not int or limit < 1:
            raise ValueError("process output limit must be a positive integer")
        data, total, eof, running = self._runtime.processes.read_output(
            process_id,
            stream=stream,
            offset=offset,
            limit=limit,
            owner=lease.identity.owner,
            session_id=lease.identity.session_id,
        )
        return ProcessOutput(
            process_id=process_id,
            stream=stream,
            offset=offset,
            total_bytes=total,
            data=data,
            eof=eof,
            running=running,
        )

    def _mark_active_process(self, process_id: str) -> None:
        """Bind one live *service* id to this invocation so cancellation stops its tree.

        Service spawn/read/write call this automatically; ordinary background jobs are never
        bound, so cancelling a log read cannot newly kill a user job.
        """
        lease = self._active_lease()
        if self._runtime.processes.service_entry(str(process_id), owner=lease.identity.owner, session_id=lease.identity.session_id) is None:
            return
        self._runtime.claim_service(lease, str(process_id))
