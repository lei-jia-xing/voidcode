from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..provider.protocol import ProviderAbortSignal


@dataclass(frozen=True, slots=True)
class _ActiveSessionKey:
    workspace: Path
    session_id: str


@dataclass(slots=True)
class _ActiveRunAbortSignal:
    _cancelled: bool = False
    _reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        if value:
            self._reason = reason


@dataclass(frozen=True, slots=True)
class ActiveRunInterruptResult:
    session_id: str
    status: Literal["interrupted", "not_active", "stale"]
    run_id: str | None = None
    reason: str | None = None

    @property
    def interrupted(self) -> bool:
        return self.status == "interrupted"

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "interrupted": self.interrupted,
            "cancelled": self.interrupted,
            "run_id": self.run_id,
            "reason": self.reason,
        }


@dataclass(slots=True)
class _ActiveRunHandle:
    run_id: str
    abort_signal: _ActiveRunAbortSignal
    metadata: dict[str, object]


class ActiveSessionRegistry:
    def __init__(self) -> None:
        self._runs: dict[_ActiveSessionKey, dict[str, _ActiveRunHandle]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _latest_handle(handles: dict[str, _ActiveRunHandle]) -> _ActiveRunHandle | None:
        if not handles:
            return None
        return next(reversed(handles.values()))

    def register(
        self,
        *,
        workspace: Path,
        session_id: str,
        run_id: str,
        metadata: dict[str, object],
    ) -> ProviderAbortSignal:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        abort_signal = _ActiveRunAbortSignal()
        with self._lock:
            handles = self._runs.setdefault(key, {})
            handles[run_id] = _ActiveRunHandle(
                run_id=run_id,
                abort_signal=abort_signal,
                metadata=dict(metadata),
            )
        return abort_signal

    def remember_metadata(
        self,
        *,
        workspace: Path,
        session_id: str,
        metadata: dict[str, object],
        run_id: str | None = None,
    ) -> None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            handles = self._runs.get(key)
            if handles is None:
                return
            handle = handles.get(run_id) if run_id is not None else self._latest_handle(handles)
            if handle is None:
                return
            handle.metadata = dict(metadata)

    def unregister(self, *, workspace: Path, session_id: str, run_id: str | None = None) -> None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            handles = self._runs.get(key)
            if handles is None:
                return
            if run_id is None:
                self._runs.pop(key, None)
                return
            handles.pop(run_id, None)
            if not handles:
                self._runs.pop(key, None)

    def contains(self, *, workspace: Path, session_id: str) -> bool:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            return key in self._runs

    def metadata(self, *, workspace: Path, session_id: str) -> dict[str, object] | None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            handles = self._runs.get(key)
            handle = self._latest_handle(handles) if handles is not None else None
            return dict(handle.metadata) if handle is not None else None

    def abort_signal(
        self,
        *,
        workspace: Path,
        session_id: str,
        run_id: str,
    ) -> ProviderAbortSignal | None:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            handles = self._runs.get(key)
            if handles is None:
                return None
            handle = handles.get(run_id)
            if handle is None:
                return None
            return handle.abort_signal

    def interrupt(
        self,
        *,
        workspace: Path,
        session_id: str,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> ActiveRunInterruptResult:
        key = _ActiveSessionKey(workspace=workspace, session_id=session_id)
        with self._lock:
            handles = self._runs.get(key)
            handle = self._latest_handle(handles) if handles is not None else None
            if handle is None:
                return ActiveRunInterruptResult(
                    session_id=session_id,
                    status="not_active",
                    run_id=run_id,
                    reason=reason,
                )
            if run_id is not None:
                requested_handle = handles.get(run_id) if handles is not None else None
                if requested_handle is None:
                    return ActiveRunInterruptResult(
                        session_id=session_id,
                        status="stale",
                        run_id=handle.run_id,
                        reason=reason,
                    )
                handle = requested_handle
            if run_id is not None and handle.run_id != run_id:
                return ActiveRunInterruptResult(
                    session_id=session_id,
                    status="stale",
                    run_id=handle.run_id,
                    reason=reason,
                )
            handle.abort_signal.set_cancelled(True, reason=reason)
            return ActiveRunInterruptResult(
                session_id=session_id,
                status="interrupted",
                run_id=handle.run_id,
                reason=reason,
            )


ACTIVE_SESSION_REGISTRY = ActiveSessionRegistry()

# Compatibility alias for callers that reached into the old service module internals.
_ACTIVE_SESSION_REGISTRY = ACTIVE_SESSION_REGISTRY
_ActiveSessionRegistry = ActiveSessionRegistry
