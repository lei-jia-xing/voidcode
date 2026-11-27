"""One runtime generation's process registry: runs, services, and cleanup."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.services import ProcessAlreadyRunningError, ProcessHandle, ProcessResult, ProcessStream

from ..sandbox import SandboxBackend, build_plan, scratch_dir
from ._background import (
    _Background,
    _cancel_entry,
    _drain_bucket,
    _entry_effects,
    _entry_settled,
    _feed_frame,
    _read_entry_output,
    _refresh_tree,
    _write_entry,
    stop_service_entries,
)
from ._exec import _decode_argv, _GroupTable, run_oneshot, stop_process
from ._host import ProcessHost
from ._procfs import process_start_identity
from ._timing import _READER_GRACE_SECONDS


class ProcessRegistry:
    """One runtime generation's processes: one-shot runs, services, and the shared host delegate."""

    def __init__(self, host: ProcessHost | None = None) -> None:
        # One-shot `run()` children only: detached background jobs are owned by the core-lifetime
        # host (they survive a generation swap), and service children live in their own sessions so
        # a generation retire stops exactly its tree without touching the shared core group.
        self._groups = _GroupTable()
        self._services: dict[str, _Background] = {}
        self._lock = threading.Lock()
        self._host = host if host is not None else ProcessHost()

    def live_group_count(self) -> int:
        return self._groups.count()

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        stdin: bytes | str | None,
        timeout_seconds: float | None,
        max_output_bytes: int,
        cancel_event: threading.Event | None = None,
        on_output: Callable[[ProcessStream, bytes], None] | None = None,
        sandbox: SandboxBackend | None = None,
        workspace: Path | None = None,
    ) -> ProcessResult:
        """Run one command to completion, a timeout, or a lease cancellation."""
        return run_oneshot(
            self._groups,
            argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            cancel_event=cancel_event,
            on_output=on_output,
            sandbox=sandbox,
            workspace=workspace,
        )

    def reap_all(self) -> None:
        """Kill and reap every one-shot `run()` group; other supervisors own their children.

        Detached background jobs live in the core-lifetime ``ProcessHost`` (stopped only by an
        explicit owner shutdown), and service children are stopped by `stop_services` per owned
        tree. This registry still leaves no *one-shot* child behind.
        """
        processes = self._groups.take_all()
        drain_until = time.monotonic() + _READER_GRACE_SECONDS
        for process in processes:
            if process.poll() is None:
                stop_process(process, drain_until=drain_until)

    # ---- background processes -----------------------------------------------------------------
    # A background process outlives the invocation that started it, so it is supervised instead of
    # awaited: the core owns the live process and its group, the caller owns the identity it
    # persists (`ProcessHandle`) and the log paths it asked for.

    def spawn(
        self,
        argv: Sequence[str],
        *,
        owner: str,
        session_id: str,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        stdin: bool,
        stdout_path: Path,
        stderr_path: Path,
        process_id: str | None = None,
        service: bool = False,
        query_service: bool = False,
        sandbox: SandboxBackend | None = None,
        workspace: Path | None = None,
    ) -> ProcessHandle:
        """Start one supervised process, streaming its output into the caller's paths.

        Detached background jobs are delegated to the shared core-lifetime ``ProcessHost`` so
        they survive a generation swap. ``service=True`` starts a generation-owned helper in its
        own session; a generation retire stops exactly that service tree.
        """
        if query_service and not service:
            raise ValueError("query_service requires service=True")
        identifier = process_id or f"{'svc' if service else 'proc'}-{uuid4().hex[:16]}"
        self._ensure_identifier_available(identifier, owner=owner, session_id=session_id)
        if not service:
            return self._host.spawn(
                argv,
                owner=owner,
                session_id=session_id,
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                process_id=identifier,
                sandbox=sandbox,
                workspace=workspace,
            )
        command = _decode_argv(argv, caller="spawn_service_process")
        for path in (stdout_path, stderr_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        frame: bytes | None = None
        effects_path: Path | None = None
        if sandbox is not None:
            anchor = str(workspace.resolve()) if workspace is not None else str((cwd or Path.cwd()).resolve())
            child_cwd = str(cwd.resolve()) if cwd is not None else anchor
            # The supervisor's cumulative sidecar lives next to the logs (runtime-owned spool).
            effects_path = stdout_path.parent / f"{identifier}.effects.json"
            helper_argv, helper_cwd, helper_env, frame = build_plan(
                sandbox,
                argv=tuple(command),
                cwd=child_cwd,
                env_overrides={str(key): str(value) for key, value in dict(env or {}).items()},
                workspace=anchor,
                scratch=scratch_dir(anchor),
                effects_path=str(effects_path),
            )
            command = list(helper_argv)
            child_env: dict[str, str] | None = helper_env
            cwd = Path(helper_cwd)
        else:
            child_env = None if env is None else {**os.environ, **{str(key): str(value) for key, value in env.items()}}
        return self._spawn_service(
            command,
            identifier=identifier,
            owner=owner,
            session_id=session_id,
            cwd=cwd,
            child_env=child_env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            frame=frame,
            effects_path=effects_path,
            query_service=query_service,
        )

    def _ensure_identifier_available(self, identifier: str, *, owner: str, session_id: str) -> None:
        """Refuse an id that another owner/session holds or that is already running."""
        background = self._host.existing(identifier)
        with self._lock:
            service = self._services.get(identifier)
        for existing in (background, service):
            if existing is None:
                continue
            if existing.owner != owner or existing.session_id != session_id:
                raise ValueError(f"background process id {identifier!r} belongs to another owner or session")
            if not _entry_settled(existing):
                raise ProcessAlreadyRunningError(identifier)

    def _spawn_service(
        self,
        command: list[str],
        *,
        identifier: str,
        owner: str,
        session_id: str,
        cwd: Path | None,
        child_env: dict[str, str] | None,
        stdout_path: Path,
        stderr_path: Path,
        frame: bytes | None = None,
        effects_path: Path | None = None,
        query_service: bool = False,
    ) -> ProcessHandle:
        """Start one service child in its own session."""
        try:
            with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
                server = subprocess.Popen(
                    command,
                    cwd=None if cwd is None else str(cwd),
                    env=child_env,
                    stdin=subprocess.PIPE,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
        except OSError as exc:
            raise ValueError(f"failed to start service process: {exc}") from exc
        if frame is not None:
            # Services keep the stdin pipe (frame first, then caller bytes): the helper
            # reads its one frame and the supervisor forwards the rest to the child.
            _feed_frame(server, frame)
        entry = _Background(
            process_id=identifier,
            owner=owner,
            session_id=session_id,
            pid=server.pid,
            start_time=process_start_identity(server.pid),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            process=server,
            own_group=True,
            query_service=bool(query_service),
            effects_path=effects_path,
        )
        with self._lock:
            replaced = self._services.get(identifier)
            if replaced is not None:
                # Defensive only: current spawns never register service pids here.
                self._groups.discard(replaced.pid)
            self._services[identifier] = entry
        _refresh_tree(entry)
        return entry.handle()

    def process_effects(self, process_id: str, *, owner: str, session_id: str) -> ObservedEffects | None:
        """Cumulative supervisor sidecar for one owner- and session-scoped process; None when unconfined."""
        service = self.service_entry(process_id, owner=owner, session_id=session_id)
        if service is not None:
            return _entry_effects(service)
        return self._host.process_effects(process_id, owner=owner, session_id=session_id)

    def service_entry(self, process_id: str, *, owner: str, session_id: str) -> _Background | None:
        """One generation service entry, or None when not owned by this owner and session."""
        with self._lock:
            entry = self._services.get(process_id)
        if entry is None or entry.owner != owner or entry.session_id != session_id:
            return None
        return entry

    def is_query_service(self, process_id: str, *, owner: str, session_id: str) -> bool:
        """Whether `process_id` names a read-only query service owned by `(owner, session_id)`."""
        entry = self.service_entry(process_id, owner=owner, session_id=session_id)
        return entry is not None and entry.query_service

    def list_background(self, *, owner: str, session_id: str) -> tuple[ProcessHandle, ...]:
        with self._lock:
            services = [entry for entry in self._services.values() if entry.owner == owner and entry.session_id == session_id]
        return (*self._host.list_background(owner=owner, session_id=session_id), *(entry.handle() for entry in services))

    def write(
        self,
        process_id: str,
        data: bytes,
        *,
        owner: str,
        session_id: str,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> int:
        """Write stdin without ever blocking this thread past the deadline or cancellation.

        A generation-owned service is written here; any other id is delegated to the shared
        background host. The returned count is the exact bytes accepted.
        """
        service = self.service_entry(process_id, owner=owner, session_id=session_id)
        if service is None:
            return self._host.write(
                process_id,
                data,
                owner=owner,
                session_id=session_id,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        return _write_entry(process_id, service, data, deadline=deadline, cancel_event=cancel_event)

    def read_output(
        self,
        process_id: str,
        *,
        stream: str,
        offset: int,
        limit: int,
        owner: str,
        session_id: str,
    ) -> tuple[bytes, int, bool, bool]:
        service = self.service_entry(process_id, owner=owner, session_id=session_id)
        if service is None:
            return self._host.read_output(
                process_id,
                stream=stream,
                offset=offset,
                limit=limit,
                owner=owner,
                session_id=session_id,
            )
        return _read_entry_output(process_id, service, stream=stream, offset=offset, limit=limit)

    def cancel(self, process_id: str, *, owner: str, session_id: str) -> bool:
        """Stop one owner- and session-scoped process and its owned descendants."""
        service = self.service_entry(process_id, owner=owner, session_id=session_id)
        if service is None:
            return self._host.cancel(process_id, owner=owner, session_id=session_id)
        return _cancel_entry(service)

    def stop_services(self) -> None:
        """Stop every service process this generation started, plus its recorded descendants.

        Called by the runtime's own cleanup on retire/reload/owner-shutdown. Each service owns
        its session, so the stop sweeps exactly its group plus the recorded tree — never the
        shared core group. Detached background jobs belong to the core-lifetime host and are
        never touched here.
        """
        entries = _drain_bucket(self._lock, self._services)
        drain_until = time.monotonic() + _READER_GRACE_SECONDS
        stop_service_entries(entries, drain_until)
