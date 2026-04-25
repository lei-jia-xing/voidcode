from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .config import RuntimeConfig, user_runtime_config_path
from .contracts import WorkspaceRegistrySnapshot, WorkspaceSummary
from .session import StoredSessionSummary
from .task import StoredBackgroundTaskSummary

_RECENT_WORKSPACES_LIMIT = 10


class WorkspaceOpenError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class WorkspaceRuntimeHandle(Protocol):
    def list_sessions(self) -> tuple[StoredSessionSummary, ...]: ...

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]: ...


class WorkspaceRuntimeFactory(Protocol):
    def __call__(self, workspace: Path) -> WorkspaceRuntimeHandle: ...


@dataclass(frozen=True, slots=True)
class WorkspaceCandidate:
    path: Path
    available: bool
    last_opened_at: int | None = None

    def as_summary(self, *, current: bool = False) -> WorkspaceSummary:
        return WorkspaceSummary(
            path=str(self.path),
            label=self.path.name or str(self.path),
            available=self.available,
            current=current,
            last_opened_at=self.last_opened_at,
        )


class SingleWorkspaceRuntimeCoordinator:
    def __init__(
        self,
        *,
        initial_workspace: Path,
        runtime_factory: WorkspaceRuntimeFactory,
        config: RuntimeConfig | None = None,
    ) -> None:
        resolved_workspace = initial_workspace.resolve()
        if not resolved_workspace.is_dir():
            raise WorkspaceOpenError(
                f"workspace path must be an existing directory: {resolved_workspace}",
                code="invalid_workspace",
                status_code=400,
            )
        self._runtime_factory = runtime_factory
        self._config = config
        self._lock = threading.RLock()
        self._active_requests = 0
        self._current_workspace = resolved_workspace
        self._runtime: WorkspaceRuntimeHandle | None = None
        self._recent_workspaces = self._load_recent_workspaces()
        self._remember_workspace_locked(resolved_workspace)

    @property
    def current_workspace(self) -> Path:
        with self._lock:
            return self._current_workspace

    def runtime(self):
        with self._lock:
            if self._runtime is None:
                self._runtime = self._runtime_factory(self._current_workspace)
            return self._runtime

    def owns_runtime(self, runtime: object) -> bool:
        with self._lock:
            return runtime is self._runtime

    def close(self) -> None:
        with self._lock:
            runtime = self._runtime
            self._runtime = None
        if runtime is None:
            return
        exit_method = getattr(runtime, "__exit__", None)
        if callable(exit_method):
            exit_method(None, None, None)

    @contextmanager
    def active_request(self) -> Iterator[None]:
        with self._lock:
            self._active_requests += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_requests = max(0, self._active_requests - 1)

    def snapshot(self) -> WorkspaceRegistrySnapshot:
        with self._lock:
            current = self._current_workspace
            recent_candidates = tuple(self._recent_workspaces)
        return WorkspaceRegistrySnapshot(
            current=WorkspaceCandidate(path=current, available=current.is_dir()).as_summary(
                current=True
            ),
            recent=tuple(
                candidate.as_summary(current=candidate.path == current)
                for candidate in recent_candidates
            ),
            candidates=self._browse_candidates(current),
        )

    def open_workspace(self, raw_path: str) -> WorkspaceRegistrySnapshot:
        candidate = Path(raw_path).expanduser().resolve()
        if not candidate.is_dir():
            raise WorkspaceOpenError(
                f"workspace path must be an existing directory: {candidate}",
                code="invalid_workspace",
                status_code=400,
            )
        with self._lock:
            self._assert_idle_locked()
            previous_runtime = self._runtime if candidate != self._current_workspace else None
            if previous_runtime is not None:
                self._runtime = None
            self._current_workspace = candidate
            self._remember_workspace_locked(candidate)
        if previous_runtime is not None:
            exit_method = getattr(previous_runtime, "__exit__", None)
            if callable(exit_method):
                exit_method(None, None, None)
        return self.snapshot()

    def _assert_idle_locked(self) -> None:
        if self._active_requests > 0:
            raise WorkspaceOpenError(
                "workspace switch rejected while a runtime request is active",
                code="workspace_busy",
                status_code=409,
            )
        runtime = self.runtime()
        active_session = next(
            (
                session
                for session in runtime.list_sessions()
                if session.status in {"running", "waiting"}
            ),
            None,
        )
        if active_session is not None:
            raise WorkspaceOpenError(
                "workspace switch rejected while a run or approval is still active",
                code="workspace_busy",
                status_code=409,
            )
        active_task = next(
            (
                task
                for task in runtime.list_background_tasks()
                if task.status in {"queued", "running"}
            ),
            None,
        )
        if active_task is not None:
            raise WorkspaceOpenError(
                "workspace switch rejected while a delegated task is still active",
                code="workspace_busy",
                status_code=409,
            )

    def _browse_candidates(self, current_workspace: Path) -> tuple[WorkspaceSummary, ...]:
        parent = (
            current_workspace.parent
            if current_workspace.parent != current_workspace
            else current_workspace
        )
        try:
            entries = sorted(
                (
                    entry.resolve()
                    for entry in parent.iterdir()
                    if entry.is_dir() and not entry.name.startswith(".")
                ),
                key=lambda item: (item.name.lower(), str(item)),
            )
        except OSError:
            return ()
        if current_workspace in entries:
            ordered_entries = [current_workspace]
            ordered_entries.extend(entry for entry in entries if entry != current_workspace)
        else:
            ordered_entries = [current_workspace, *entries]
        return tuple(
            WorkspaceCandidate(path=entry, available=True).as_summary(
                current=entry == current_workspace
            )
            for entry in ordered_entries[:_RECENT_WORKSPACES_LIMIT]
        )

    def _remember_workspace_locked(self, workspace: Path) -> None:
        existing = [item for item in self._recent_workspaces if item.path != workspace]
        self._recent_workspaces = (
            WorkspaceCandidate(path=workspace, available=workspace.is_dir(), last_opened_at=1),
            *existing,
        )[:_RECENT_WORKSPACES_LIMIT]
        self._save_recent_workspaces(self._recent_workspaces)

    @staticmethod
    def _load_recent_workspaces() -> tuple[WorkspaceCandidate, ...]:
        payload = _read_user_config_json()
        raw_web = payload.get("web")
        if not isinstance(raw_web, dict):
            return ()
        typed_web = cast(dict[str, object], raw_web)
        raw_recent = typed_web.get("recent_workspaces")
        if not isinstance(raw_recent, list):
            return ()
        recent_entries = cast(list[object], raw_recent)
        workspaces: list[WorkspaceCandidate] = []
        for index, raw_path in enumerate(recent_entries):
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            path = Path(raw_path).expanduser().resolve()
            workspaces.append(
                WorkspaceCandidate(
                    path=path,
                    available=path.is_dir(),
                    last_opened_at=len(recent_entries) - index,
                )
            )
        deduped: list[WorkspaceCandidate] = []
        seen: set[Path] = set()
        for workspace in workspaces:
            if workspace.path in seen:
                continue
            seen.add(workspace.path)
            deduped.append(workspace)
        return tuple(deduped[:_RECENT_WORKSPACES_LIMIT])

    @staticmethod
    def _save_recent_workspaces(workspaces: tuple[WorkspaceCandidate, ...]) -> None:
        config_path = user_runtime_config_path()
        payload = _read_user_config_json()
        raw_web = payload.get("web")
        web_payload: dict[str, object] = (
            dict(cast(dict[str, object], raw_web)) if isinstance(raw_web, dict) else {}
        )
        web_payload["recent_workspaces"] = [str(workspace.path) for workspace in workspaces]
        payload["web"] = web_payload
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _read_user_config_json() -> dict[str, object]:
    config_path = user_runtime_config_path()
    if not config_path.exists():
        return {}
    raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict):
        raise ValueError(f"runtime config file must contain a JSON object: {config_path}")
    return dict(cast(dict[str, object], raw_payload))
