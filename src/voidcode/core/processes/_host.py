"""Core-lifetime supervisor for detached background jobs."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.services import ProcessAlreadyRunningError, ProcessHandle

from ..sandbox import SandboxBackend, build_plan, scratch_dir
from ._background import (
    MAX_EXITED_BACKGROUND_PER_SESSION,
    _Background,
    _cancel_entry,
    _drain_bucket,
    _entry_effects,
    _entry_settled,
    _feed_frame,
    _read_entry_output,
    _stop_owned_group,
    _write_entry,
)
from ._exec import _decode_argv
from ._procfs import process_start_identity
from ._timing import _READER_GRACE_SECONDS


class ProcessHost:
    """Core-lifetime supervisor for detached background jobs.

    A host outlives plugin runtime generations: every ``ProcessRegistry`` built by the same
    Core shares it, so a reload neither loses a job nor its log paths. ``Core.close`` is the
    owner shutdown that stops these jobs; generation close stops only one-shot children and
    service processes.
    """

    def __init__(
        self,
        *,
        spool_dir: Path | None = None,
        max_exited_per_session: int = MAX_EXITED_BACKGROUND_PER_SESSION,
    ) -> None:
        self._jobs: dict[str, _Background] = {}
        self._lock = threading.Lock()
        self._spool_dir = None if spool_dir is None else Path(spool_dir)
        self._max_exited_per_session = max_exited_per_session

    @property
    def spool_dir(self) -> Path:
        """Stable runtime-owned log root, created lazily with owner-only permissions."""
        with self._lock:
            if self._spool_dir is None:
                self._spool_dir = Path(tempfile.gettempdir()) / f"voidcode-process-{uuid4().hex}"
            spool = self._spool_dir
        spool.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(spool, 0o700)
        except OSError:
            pass
        return spool

    def existing(self, process_id: str) -> _Background | None:
        """One background entry by id, ignoring ownership; used for duplicate-id checks."""
        with self._lock:
            return self._jobs.get(process_id)

    def background(self, process_id: str, *, owner: str, session_id: str) -> _Background:
        """One background job, visible only to the owner and session that started it."""
        entry = self.existing(process_id)
        if entry is None or entry.owner != owner or entry.session_id != session_id:
            raise ValueError(f"unknown background process for this owner and session: {process_id}")
        return entry

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
        sandbox: SandboxBackend | None = None,
        workspace: Path | None = None,
    ) -> ProcessHandle:
        """Start one detached background job whose identity and logs survive a reload."""
        command = _decode_argv(argv, caller="spawn_process")
        identifier = process_id or f"proc-{uuid4().hex[:16]}"
        with self._lock:
            existing = self._jobs.get(identifier)
            if existing is not None and (existing.owner != owner or existing.session_id != session_id):
                raise ValueError(f"background process id {identifier!r} belongs to another owner or session")
            if existing is not None and not _entry_settled(existing):
                raise ProcessAlreadyRunningError(identifier)
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
        try:
            # Logs are appended, never truncated: restarting the same process id keeps one history,
            # which is what makes a readable log id useful across attempts.
            with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
                process = subprocess.Popen(
                    command,
                    cwd=None if cwd is None else str(cwd),
                    env=child_env,
                    stdin=subprocess.PIPE if (stdin or frame is not None) else subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
        except OSError as exc:
            raise ValueError(f"failed to start background process: {exc}") from exc
        if frame is not None:
            _feed_frame(process, frame)
            if not stdin:
                # The frame was the only stdin the helper reads; close so a service-style
                # helper never blocks waiting for more (a stdin=True caller keeps the pipe).
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except BrokenPipeError, OSError, ValueError:
                    pass
        entry = _Background(
            process_id=identifier,
            owner=owner,
            session_id=session_id,
            pid=process.pid,
            start_time=process_start_identity(process.pid),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            process=process,
            own_group=True,
            effects_path=effects_path,
        )
        with self._lock:
            self._jobs.pop(identifier, None)
            self._jobs[identifier] = entry
            self._evict_exited(owner, session_id)
        return entry.handle()

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
        """Write stdin without ever blocking this thread past the deadline or cancellation."""
        entry = self.background(process_id, owner=owner, session_id=session_id)
        return _write_entry(process_id, entry, data, deadline=deadline, cancel_event=cancel_event)

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
        entry = self.background(process_id, owner=owner, session_id=session_id)
        return _read_entry_output(process_id, entry, stream=stream, offset=offset, limit=limit)

    def cancel(self, process_id: str, *, owner: str, session_id: str) -> bool:
        """Stop one owner- and session-scoped job and its owned descendants."""
        entry = self.background(process_id, owner=owner, session_id=session_id)
        return _cancel_entry(entry)

    def list_background(self, *, owner: str, session_id: str) -> tuple[ProcessHandle, ...]:
        with self._lock:
            self._evict_exited(owner, session_id)
            entries = [entry for entry in self._jobs.values() if entry.owner == owner and entry.session_id == session_id]
        return tuple(entry.handle() for entry in entries)

    def process_effects(self, process_id: str, *, owner: str, session_id: str) -> ObservedEffects | None:
        """Cumulative supervisor sidecar for one owner- and session-scoped job; None when unconfined."""
        return _entry_effects(self.background(process_id, owner=owner, session_id=session_id))

    def stop_background(self) -> None:
        """Stop every detached background job at owner shutdown; a reload never calls this."""
        entries = _drain_bucket(self._lock, self._jobs)
        for entry in entries:
            # One grace per job: sharing a single deadline lets an earlier TERM-ignoring job
            # consume the whole window and turns a later job's TERM into an immediate SIGKILL.
            _stop_owned_group(entry, time.monotonic() + _READER_GRACE_SECONDS)

    def _evict_exited(self, owner: str, session_id: str) -> None:
        """Drop the oldest exited identities beyond the per-owner/session cap (caller holds lock)."""
        exited = [
            process_id
            for process_id, entry in self._jobs.items()
            if entry.owner == owner and entry.session_id == session_id and _entry_settled(entry)
        ]
        overflow = len(exited) - self._max_exited_per_session
        for process_id in exited[: max(0, overflow)]:
            del self._jobs[process_id]
