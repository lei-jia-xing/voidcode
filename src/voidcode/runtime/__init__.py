from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from .active_session import ActiveRunInterruptResult
from .background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    BackgroundTaskStatus,
    StoredBackgroundTaskSummary,
)
from .contracts import (
    BackgroundTaskResult,
    RuntimeEntrypoint,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeSessionResult,
    RuntimeStreamChunk,
    RuntimeStreamChunkKind,
    StreamingRuntimeEntrypoint,
    UnknownBackgroundTaskError,
    validate_id,
)
from .events import (
    DelegatedExecutionPayload,
    DelegatedLifecycleEventPayload,
    DelegatedLifecycleMessage,
    DelegatedRoutingPayload,
    EventEnvelope,
    EventSource,
)
from .permission import ApprovalMode, PendingApproval, PermissionDecision, PermissionPolicy, PermissionResolution
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .storage import RuntimeRepositories

if TYPE_CHECKING:
    from .service import VoidCodeRuntime
    from .tool_registry import ToolRegistry
    from .transport.http import RuntimeTransportApp, create_runtime_app

__all__ = [
    "EventEnvelope",
    "EventSource",
    "DelegatedExecutionPayload",
    "DelegatedLifecycleEventPayload",
    "DelegatedLifecycleMessage",
    "DelegatedRoutingPayload",
    "BackgroundTaskRef",
    "BackgroundTaskRequestSnapshot",
    "BackgroundTaskResult",
    "BackgroundTaskState",
    "BackgroundTaskStatus",
    "UnknownBackgroundTaskError",
    "ActiveRunInterruptResult",
    "ApprovalMode",
    "PendingApproval",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionResolution",
    "RuntimeTransportApp",
    "RuntimeEntrypoint",
    "RuntimeRequest",
    "RuntimeResponse",
    "RuntimeSessionResult",
    "StreamingRuntimeEntrypoint",
    "RuntimeStreamChunk",
    "RuntimeStreamChunkKind",
    "SessionRef",
    "SessionState",
    "SessionStatus",
    "RuntimeRepositories",
    "StoredSessionSummary",
    "StoredBackgroundTaskSummary",
    "ToolRegistry",
    "VoidCodeRuntime",
    "create_runtime_app",
    "validate_id",
]


def __getattr__(name: str) -> Any:
    if name == "ToolRegistry":
        tool_registry_module = import_module(".tool_registry", __name__)
        return getattr(tool_registry_module, name)
    if name == "VoidCodeRuntime":
        service_module = import_module(".service", __name__)
        return getattr(service_module, name)
    if name in {"RuntimeTransportApp", "create_runtime_app"}:
        http_module = import_module(".transport.http", __name__)
        return getattr(http_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
