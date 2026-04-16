from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type BackgroundTaskStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class BackgroundTaskRef:
    id: str


@dataclass(frozen=True, slots=True)
class BackgroundTaskRequestSnapshot:
    prompt: str
    session_id: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    allocate_session_id: bool = False


@dataclass(frozen=True, slots=True)
class BackgroundTaskState:
    task: BackgroundTaskRef
    status: BackgroundTaskStatus = "queued"
    request: BackgroundTaskRequestSnapshot = field(
        default_factory=lambda: BackgroundTaskRequestSnapshot(prompt="")
    )
    session_id: str | None = None
    error: str | None = None
    created_at: int = 0
    updated_at: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    cancel_requested_at: int | None = None


@dataclass(frozen=True, slots=True)
class StoredBackgroundTaskSummary:
    task: BackgroundTaskRef
    status: BackgroundTaskStatus
    prompt: str
    session_id: str | None
    error: str | None
    created_at: int
    updated_at: int


def validate_background_task_id(task_id: str) -> str:
    if not task_id:
        raise ValueError("task_id must be a non-empty string")
    if "/" in task_id:
        raise ValueError("task_id must not contain '/'")
    return task_id
