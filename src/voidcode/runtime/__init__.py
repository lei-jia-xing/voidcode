from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .contracts import (
    BackgroundTaskRuntimeEntrypoint,
    RuntimeEntrypoint,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeStreamChunk,
    RuntimeStreamChunkKind,
    StreamingRuntimeEntrypoint,
)
from .events import EventEnvelope, EventSource
from .permission import PendingApproval, PermissionDecision, PermissionPolicy, PermissionResolution
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .storage import SessionStore
from .task import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
    validate_background_task_id,
)

if TYPE_CHECKING:
    from .http import RuntimeTransportApp, create_runtime_app
    from .service import ToolRegistry, VoidCodeRuntime

__all__ = [
    "EventEnvelope",
    "EventSource",
    "BackgroundTaskRef",
    "BackgroundTaskRequestSnapshot",
    "BackgroundTaskRuntimeEntrypoint",
    "BackgroundTaskState",
    "BackgroundTaskStatus",
    "PendingApproval",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionResolution",
    "RuntimeTransportApp",
    "RuntimeEntrypoint",
    "RuntimeRequest",
    "RuntimeResponse",
    "StreamingRuntimeEntrypoint",
    "RuntimeStreamChunk",
    "RuntimeStreamChunkKind",
    "SessionRef",
    "SessionState",
    "SessionStatus",
    "SessionStore",
    "StoredSessionSummary",
    "StoredBackgroundTaskSummary",
    "ToolRegistry",
    "VoidCodeRuntime",
    "create_runtime_app",
    "validate_background_task_id",
]


def __getattr__(name: str) -> Any:
    if name in {"ToolRegistry", "VoidCodeRuntime"}:
        service_module = import_module(".service", __name__)
        return getattr(service_module, name)
    if name in {"RuntimeTransportApp", "create_runtime_app"}:
        http_module = import_module(".http", __name__)
        return getattr(http_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
