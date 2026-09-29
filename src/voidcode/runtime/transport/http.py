"""Runtime HTTP transport backed by FastAPI.

This module owns the runtime's only HTTP surface. It keeps the wire contract the
hand-rolled ASGI app established — routes, methods, status codes, the error
envelope, byte-stable JSON rendering and hand-framed server-sent events — while
FastAPI owns routing, method dispatch, request-body parsing and error handling.
The client contract for those shapes lives in
:mod:`voidcode.runtime.transport.http_contract`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager, nullcontext, suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol, cast, final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from ..active_session import ActiveRunInterruptResult
from ..background.models import (
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    StoredBackgroundTaskSummary,
)
from ..background.routing import (
    SubagentRoutingIdentity,
)
from ..config import RuntimeConfig
from ..contracts import (
    AgentSummary,
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    CommandSummary,
    GitStatusSnapshot,
    NoPendingQuestionError,
    ProviderInspectResult,
    ProviderModelMetadata,
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
    ReviewChangedFile,
    ReviewFileDiff,
    ReviewTreeNode,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeResponse,
    RuntimeSessionDebugSnapshot,
    RuntimeSessionResult,
    RuntimeSessionRevertMarker,
    RuntimeStatusSnapshot,
    RuntimeStreamChunk,
    SessionEventBatch,
    SkillSummary,
    UnknownSessionError,
    WorkspaceRegistrySnapshot,
    WorkspaceReviewSnapshot,
    WorkspaceSummary,
    validate_id,
    validate_runtime_request_metadata,
)
from ..events import (
    DelegatedLifecycleEventPayload,
    EventEnvelope,
    redact_reasoning_payload,
)
from ..permission import PermissionResolution
from ..question import QuestionResponse
from ..serialization import (
    _serialize_session_ref,
    _serialize_session_state,
    serialize_revert_marker,
    serialize_session_debug_snapshot,
)
from ..service import VoidCodeRuntime
from ..session import SessionState, StoredSessionForestEntry, StoredSessionSummary
from ..storage.shared import SessionSealedError
from ..workspace import WorkspaceOpenError, WorkspaceRuntimeCoordinator
from .http_contract import (
    AfterSequenceQuery,
    EventStreamResponse,
    FollowQuery,
    HttpError,
    JsonBodyContentTypeMiddleware,
    JsonResponse,
    ShowThinkingQuery,
    StreamCompletion,
    _ApprovalResolutionRequestPayload,
    _QuestionAnswerRequestPayload,
    _RunStreamRequestPayload,
    _SessionCancelRequestPayload,
    _SessionRevertRequestPayload,
    _SettingsRequestPayload,
    _SteerSessionRequestPayload,
    _TaskSteerRequestPayload,
    _WorkspaceOpenRequestPayload,
    error_code,
    http_exception_response,
    is_api_path,
    json_response,
    request_validation_error_response,
    sse_frame,
    unhandled_exception_response,
)
from .http_models import (
    AgentSummaryBody,
    BackgroundTaskOutputBody,
    BackgroundTaskRetryBody,
    BackgroundTaskStateBody,
    BackgroundTaskSteerBody,
    BackgroundTaskSummaryBody,
    CommandSummaryBody,
    ErrorEnvelope,
    ProviderInspectBody,
    ProviderModelsBody,
    ProviderSummaryBody,
    ProviderValidationBody,
    ReviewFileDiffBody,
    RunStreamFrameBody,
    RuntimeResponseBody,
    RuntimeStatusBody,
    SessionCancelBody,
    SessionDebugBody,
    SessionEventFrameBody,
    SessionResultBody,
    SessionRevertBody,
    SessionSteerBody,
    SessionSummaryBody,
    SkillSummaryBody,
    WebSettingsBody,
    WorkspaceRegistryBody,
    WorkspaceReviewBody,
)

logger = logging.getLogger(__name__)


def _default_runtime_class() -> type[VoidCodeRuntime]:
    """Resolve the runtime class, honouring a patch on the runtime package facade."""
    runtime_module = sys.modules.get("voidcode.runtime")
    if runtime_module is not None:
        patched = runtime_module.__dict__.get("VoidCodeRuntime")
        if patched is not None:
            return cast(type[VoidCodeRuntime], patched)
    return VoidCodeRuntime


# Statuses after which the session-event follow stream closes instead of
# polling forever. ``interrupted`` is a regular terminal state (user-cancelled
# runs seal as ``interrupted`` with the ``runtime.failed{cancelled: true}``
# event), so the follow stream must close on it exactly like completed/failed.
_SESSION_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})

# Idle poll interval for the session-event follow stream. The tick itself only
# reads events past the client cursor plus the session status, so the interval
# bounds follow latency without loading the transcript again. Starlette owns the
# connection's ``receive()``, so the tick is an idle sleep rather than a
# disconnect-polling receive.
_SESSION_EVENT_FOLLOW_POLL_SECONDS = 1.0

# Upper bound on how long a closing stream waits for its producer thread. The
# runtime's ``run_stream`` is a synchronous generator with no cancellation seam,
# so a chunk already in flight cannot be interrupted; the thread is a daemon and
# stops at its next chunk boundary once the stop flag is set.
_STREAM_WORKER_JOIN_SECONDS = 0.05


async def _aclose_async_iterator(iterator: AsyncGenerator[object]) -> None:
    """Close an async generator that a response owns, swallowing teardown noise."""
    with suppress(BaseException):
        await iterator.aclose()


def _chunk_reports_failure(chunk: RuntimeStreamChunk) -> bool:
    """Whether a run-stream chunk is the terminal failure frame."""
    return chunk.event is not None and chunk.event.event_type == "runtime.failed"


def _resolved_task_output(
    task_result: BackgroundTaskResult,
    child_session_result: RuntimeSessionResult | None,
) -> str | None:
    """The output the background-task read surfaces fall back to.

    The child session's own transcript wins, then the task's summarized output,
    then its error. The three read surfaces over one task
    (``/api/tasks/{id}/output`` and ``/api/sessions/{id}/delegated-context``)
    must agree, so they share this one derivation.
    """
    if child_session_result is not None and child_session_result.output is not None:
        return child_session_result.output
    if task_result.summary_output is not None:
        return task_result.summary_output
    return task_result.error


@final
class _ClientDisconnectCancellation:
    """Cancel a streamed run exactly once, whoever notices the disconnect first.

    Both the frame generator (cancellation) and the response (a failing send)
    can be the first to observe that the client is gone, and the run must be
    cancelled exactly once either way.
    """

    __slots__ = ("_runtime", "_session_id", "_cancelled")

    def __init__(self, runtime: RuntimeTransport, session_id: str) -> None:
        self._runtime = runtime
        self._session_id = session_id
        self._cancelled = False

    def __call__(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        try:
            self._runtime.cancel_session(self._session_id, reason="client_disconnected")
        except Exception:
            logger.exception("failed to cancel run after client disconnect")


@final
class _SessionStateEmitter:
    """Emit run-stream session state only when it actually changes.

    The run stream emits one frame per provider delta, and the session metadata
    blob (``context_window``, ``agent_capability_snapshot``, ``runtime_config``,
    ``pending_messages``, ...) is tens of kilobytes. Re-serializing and
    re-sending it on every delta multiplied wire traffic and client-side parsing
    by the metadata size while the value was unchanged.

    The wire field keeps its shape: a frame carries the full serialized session
    state on the first emission of a response and whenever it changes, and
    ``null`` otherwise. Consumers already treat ``null`` as "keep the state you
    have".
    """

    __slots__ = ("_emitted",)

    def __init__(self) -> None:
        self._emitted: SessionState | None = None

    def serialize(self, session: SessionState) -> dict[str, object] | None:
        if self._emitted is not None and _session_states_serialize_identically(self._emitted, session):
            return None
        payload = _serialize_session_state(session)
        # Snapshot what was emitted instead of holding the caller's live state:
        # an in-place metadata update must be visible on the next frame, and the
        # copy is paid once per emission rather than once per frame.
        self._emitted = SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata=deepcopy(session.metadata),
        )
        return payload


def _session_states_serialize_identically(previous: SessionState, current: SessionState) -> bool:
    """Whether two session states produce the same serialized payload.

    Mirrors ``_serialize_session_state`` field for field, so a changed session
    ref, status, turn, or metadata always re-emits the full state. The metadata
    comparison is by value (not identity) so an in-place metadata update still
    re-emits, and it stays an order of magnitude cheaper than re-serializing the
    metadata blob it skips.
    """
    return (
        previous.session == current.session
        and previous.status == current.status
        and previous.turn == current.turn
        and previous.metadata == current.metadata
    )


class RuntimeTransport(Protocol):
    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...

    def start_background_task(self, request: RuntimeRequest) -> BackgroundTaskState: ...

    def authorize_background_task_owner(self, task_id: str, *, parent_session_id: str | None) -> None: ...

    def load_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult: ...

    def load_background_task_result_by_child_session(self, *, child_session_id: str) -> BackgroundTaskResult | None: ...

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def list_background_tasks_by_parent_session(self, *, parent_session_id: str) -> tuple[StoredBackgroundTaskSummary, ...]: ...

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def retry_background_task(self, task_id: str) -> BackgroundTaskState: ...

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState: ...

    def queue_steering(self, session_id: str, content: str) -> tuple[dict[str, object], ...]: ...

    def cancel_session(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> ActiveRunInterruptResult: ...

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]: ...

    def session_forest(self) -> tuple[StoredSessionForestEntry, ...]: ...

    def web_settings(self) -> dict[str, object]: ...

    def update_web_settings(
        self,
        *,
        provider: str | None = None,
        provider_api_key: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]: ...

    def list_provider_summaries(self) -> tuple[ProviderSummary, ...]: ...

    def provider_models_result(self, provider_name: str) -> ProviderModelsResult: ...

    def inspect_provider(self, provider_name: str) -> ProviderInspectResult: ...

    def validate_provider_credentials(self, provider_name: str) -> ProviderValidationResult: ...

    def list_agent_summaries(self) -> tuple[AgentSummary, ...]: ...

    def list_skill_summaries(self) -> tuple[SkillSummary, ...]: ...

    def list_command_summaries(self) -> tuple[CommandSummary, ...]: ...

    def current_status(self) -> RuntimeStatusSnapshot: ...

    def retry_mcp_connections(self) -> RuntimeStatusSnapshot: ...

    def review_snapshot(self) -> WorkspaceReviewSnapshot: ...

    def review_diff(self, path: str) -> ReviewFileDiff: ...

    def session_result(self, *, session_id: str) -> RuntimeSessionResult: ...

    def replay_session(self, *, session_id: str) -> RuntimeResponse: ...

    def session_events_after(self, *, session_id: str, after_sequence: int) -> SessionEventBatch: ...

    def session_debug_snapshot(self, *, session_id: str) -> RuntimeSessionDebugSnapshot: ...

    def revert_session(self, *, session_id: str, sequence: int) -> RuntimeSessionRevertMarker: ...

    def undo_session(self, *, session_id: str) -> RuntimeSessionRevertMarker: ...

    def unrevert_session(self, *, session_id: str) -> RuntimeSessionRevertMarker | None: ...

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse: ...

    def answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> RuntimeResponse: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


def _runtime_request_from(payload: _RunStreamRequestPayload) -> RuntimeRequest:
    """Build the runtime request, applying the runtime's own boundary checks.

    Session ids and request metadata are runtime-owned boundary inputs, so their
    failures are reported as the requested-value errors the transport has always
    produced rather than as field validation.
    """
    session_id = payload.session_id
    if session_id is not None:
        validate_id(session_id)

    parent_session_id = payload.parent_session_id
    if parent_session_id is not None:
        validate_id(
            parent_session_id,
            field_name="parent_session_id",
        )

    return RuntimeRequest(
        prompt=payload.prompt,
        session_id=session_id,
        parent_session_id=parent_session_id,
        metadata=validate_runtime_request_metadata(payload.metadata),
        allocate_session_id=session_id is None,
    )


# Every failing route answers the transport's error envelope, so each operation
# documents that body for the statuses it can actually produce plus one
# catch-all. The catch-all also keeps FastAPI from advertising the 422 it adds by
# default: this transport answers validation failures with 400.
_ERROR_ENVELOPE_DESCRIPTION = (
    "Any failing response answers this envelope: 400 for a validation failure, 404 for an unknown path, session or "
    "task, 405 for a wrong method, 409 for a conflict, and 500 for an unhandled failure."
)

# The frames of a server-sent-events route are not expressible as an OpenAPI
# response body, so the route publishes the frame model next to the media type
# and the framing itself is documented in docs/contracts/http-response-schema.md.
_SSE_SUCCESS_DESCRIPTION = (
    "A hand-framed server-sent-events stream: the body is one `data: <json>\\n\\n` frame per event, and every frame "
    "is the referenced model. OpenAPI cannot describe an event-stream body, so the frame contract is published here "
    "and pinned by tests/integration/test_http_response_schema.py."
)


# The media type the JSON response class renders; the transport's error envelope
# always uses it, including on the SSE routes whose *failures* are plain JSON.
_JSON_MEDIA_TYPE = JsonResponse.media_type

# A route that answers its own payload with an extra status (a provider that is
# not configured answers 409 with its inspection payload, not with the error
# envelope) documents that status with the same model.
_ALTERNATE_STATUS_DESCRIPTION = (
    "The same body, answered with a non-success status: the runtime reports a provider that is neither configured nor "
    "validated with 409 instead of failing the request."
)


def _error_responses(statuses: tuple[int, ...], *, media_type: str | None = None) -> dict[int | str, dict[str, object]]:
    """The documented error envelope for one route: named statuses plus a catch-all.

    ``media_type`` pins the media the envelope is served as. FastAPI documents a
    ``model=`` response under the *route's* response media type, which would
    describe an SSE route's JSON failure body as ``text/event-stream``; passing
    the JSON media type makes the entry name it outright. The ``ErrorEnvelope``
    component is published by the JSON routes either way.
    """
    envelope: dict[str, object] = {"model": ErrorEnvelope}
    if media_type is not None:
        envelope = {"content": {media_type: {"schema": {"$ref": f"#/components/schemas/{ErrorEnvelope.__name__}"}}}}
    responses: dict[int | str, dict[str, object]] = {status: dict(envelope) for status in statuses}
    responses["default"] = {**envelope, "description": _ERROR_ENVELOPE_DESCRIPTION}
    return responses


def _sse_success_response(frame: type[BaseModel]) -> dict[str, object]:
    """The 200 response of an SSE route: media type plus the frame model reference.

    ``itemSchema`` is the extension FastAPI itself uses for SSE routes; the
    ``$ref`` resolves because the route also declares the frame model as its
    ``response_model``, which publishes it in ``components.schemas``.
    """
    return {
        "description": _SSE_SUCCESS_DESCRIPTION,
        "content": {"text/event-stream": {"itemSchema": {"$ref": f"#/components/schemas/{frame.__name__}"}}},
    }


@final
class RuntimeTransportApp(FastAPI):
    """The runtime's HTTP transport as a FastAPI application.

    Construction and routing are frozen for the test suite: the app is still
    built through ``create_runtime_app`` or ``RuntimeTransportApp(runtime_factory=,
    workspace_coordinator=, frontend_dist=)``, every route keeps its path,
    method, status code and body shape, and the streaming paths stay
    hand-framed.
    """

    _runtime_factory: Callable[[], RuntimeTransport]
    _workspace_coordinator: WorkspaceRuntimeCoordinator | None

    def __init__(
        self,
        *,
        runtime_factory: Callable[[], RuntimeTransport],
        workspace_coordinator: WorkspaceRuntimeCoordinator | None = None,
        frontend_dist: Path | None = None,
    ) -> None:
        super().__init__(
            title="VoidCode runtime API",
            description=(
                "Local-first VoidCode runtime. Every API response is JSON (or a hand-framed SSE stream) and every "
                '/api error uses the {"error", "code"} envelope: validation failures are 400, unmatched paths 404, '
                "wrong methods 405 and unhandled failures 500."
            ),
            docs_url=None,
            redoc_url=None,
            # The interactive UIs stay off because they load assets from a CDN
            # and this server is local-first by design. FastAPI's own OpenAPI
            # route is off too: the transport serves the document itself so it
            # keeps the one JSON renderer (sorted keys, explicit charset).
            openapi_url=None,
            redirect_slashes=False,
            default_response_class=JsonResponse,
            lifespan=self._lifespan,
            middleware=[Middleware(JsonBodyContentTypeMiddleware)],
            exception_handlers={
                HTTPException: http_exception_response,
                RequestValidationError: request_validation_error_response,
                # Starlette routes a handler for ``Exception`` to its outermost
                # server-error middleware: the transport's envelope is sent, and
                # the exception is re-raised afterwards so the server still logs
                # the traceback.
                Exception: unhandled_exception_response,
            },
        )
        self._runtime_factory = runtime_factory
        self._workspace_coordinator = workspace_coordinator
        self._frontend_dist = frontend_dist
        # Paths no API route claimed fall through to the static/SPA handler,
        # which is also what keeps unknown ``/api`` paths a JSON 404.
        self.router.default = self._serve_unmatched_path
        self._register_routes()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "lifespan"):
            raise RuntimeError(f"unsupported scope type: {scope_type!r}")
        await super().__call__(scope, receive, send)

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        """Release the workspace coordinator on server shutdown.

        ``server.py`` starts uvicorn with lifecycle handling enabled, so the
        served process runs this hook and the coordinator is released exactly
        once. Embedders and tests that drive a lifespan scope reach it too.
        """
        yield
        if self._workspace_coordinator is not None:
            self._workspace_coordinator.close()

    def _register_routes(self) -> None:
        """The transport's route table.

        Paths, methods and status codes are the hand-rolled transport's; ``tag``,
        ``summary`` and ``response_model`` are what the OpenAPI document at
        ``/api/openapi.json`` publishes. The response models come from
        :mod:`voidcode.runtime.transport.http_models` and describe exactly what
        the ``_serialize_*`` projections emit; every endpoint returns its own
        ``Response``, so FastAPI documents the shapes without touching the wire.
        An SSE route passes its frame model as ``streaming_frame``: the frame is
        published next to the ``text/event-stream`` media type because OpenAPI
        cannot describe an event-stream body.
        """

        def route(
            path: str,
            endpoint: Callable[..., Awaitable[Response]],
            *,
            methods: list[str],
            tag: str,
            summary: str,
            response_model: Any = None,
            status_code: int = 200,
            error_statuses: tuple[int, ...] = (),
            alternate_statuses: tuple[int, ...] = (),
            streaming_frame: type[BaseModel] | None = None,
            in_schema: bool = True,
        ) -> None:
            response_class: type[Response] = JsonResponse
            # An SSE route's failures are JSON, so its error entries name the
            # media type instead of inheriting the event-stream one.
            envelope_media_type = None if streaming_frame is None else _JSON_MEDIA_TYPE
            responses = _error_responses(error_statuses, media_type=envelope_media_type)
            for status in alternate_statuses:
                responses[status] = {"model": response_model, "description": _ALTERNATE_STATUS_DESCRIPTION}
            if streaming_frame is not None:
                responses[200] = _sse_success_response(streaming_frame)
                response_class = EventStreamResponse
            self.add_api_route(
                path,
                endpoint,
                methods=methods,
                tags=[tag],
                summary=summary,
                response_model=streaming_frame or response_model,
                status_code=status_code,
                responses=responses,
                response_class=response_class,
                include_in_schema=in_schema,
            )

        route(
            "/api/openapi.json",
            self._handle_openapi_document,
            methods=["GET"],
            tag="runtime",
            summary="Read this API surface as an OpenAPI document",
            in_schema=False,
        )

        route(
            "/api/runtime/run/stream",
            self._handle_run_stream,
            methods=["POST"],
            tag="runtime",
            summary="Run a prompt and stream ordered events as SSE",
            streaming_frame=RunStreamFrameBody,
            error_statuses=(400,),
        )
        route(
            "/api/sessions",
            self._handle_list_sessions,
            methods=["GET"],
            tag="sessions",
            summary="List persisted main sessions",
            response_model=list[SessionSummaryBody],
        )
        route(
            "/api/tasks",
            self._handle_list_background_tasks,
            methods=["GET"],
            tag="tasks",
            summary="List background tasks",
            response_model=list[BackgroundTaskSummaryBody],
        )
        route(
            "/api/tasks",
            self._handle_start_background_task,
            methods=["POST"],
            tag="tasks",
            summary="Start a background task",
            response_model=BackgroundTaskStateBody,
            status_code=201,
            error_statuses=(400,),
        )
        route(
            "/api/settings",
            self._handle_get_settings,
            methods=["GET"],
            tag="settings",
            summary="Read runtime web settings",
            response_model=WebSettingsBody,
        )
        route(
            "/api/settings",
            self._handle_update_settings,
            methods=["POST"],
            tag="settings",
            summary="Update runtime web settings",
            response_model=WebSettingsBody,
            error_statuses=(400,),
        )
        route(
            "/api/workspaces",
            self._handle_list_workspaces,
            methods=["GET"],
            tag="workspaces",
            summary="List the workspace registry",
            response_model=WorkspaceRegistryBody,
            error_statuses=(404,),
        )
        route(
            "/api/workspaces/open",
            self._handle_open_workspace,
            methods=["POST"],
            tag="workspaces",
            summary="Open or switch the active workspace",
            response_model=WorkspaceRegistryBody,
            error_statuses=(400, 404, 409),
        )
        route(
            "/api/providers",
            self._handle_list_providers,
            methods=["GET"],
            tag="providers",
            summary="List providers",
            response_model=list[ProviderSummaryBody],
        )
        route(
            "/api/agents",
            self._handle_list_agents,
            methods=["GET"],
            tag="runtime",
            summary="List available agents",
            response_model=list[AgentSummaryBody],
        )
        route(
            "/api/skills",
            self._handle_list_skills,
            methods=["GET"],
            tag="runtime",
            summary="List available skills",
            response_model=list[SkillSummaryBody],
        )
        route(
            "/api/commands",
            self._handle_list_commands,
            methods=["GET"],
            tag="runtime",
            summary="List available commands",
            response_model=list[CommandSummaryBody],
        )
        route(
            "/api/status",
            self._handle_get_status,
            methods=["GET"],
            tag="runtime",
            summary="Read the runtime status snapshot",
            response_model=RuntimeStatusBody,
        )
        route(
            "/api/status/mcp/retry",
            self._handle_retry_mcp,
            methods=["POST"],
            tag="runtime",
            summary="Retry MCP connections",
            response_model=RuntimeStatusBody,
            error_statuses=(400,),
        )
        route(
            "/api/review",
            self._handle_get_review,
            methods=["GET"],
            tag="review",
            summary="Read the workspace review snapshot",
            response_model=WorkspaceReviewBody,
        )
        route(
            "/api/review/diff/{path:path}",
            self._handle_get_review_diff,
            methods=["GET"],
            tag="review",
            summary="Read one file diff",
            response_model=ReviewFileDiffBody,
            error_statuses=(400, 404),
        )
        route(
            "/api/tasks/{task_id}",
            self._handle_background_task_status,
            methods=["GET"],
            tag="tasks",
            summary="Read one background task",
            response_model=BackgroundTaskStateBody,
            error_statuses=(404,),
        )
        route(
            "/api/tasks/{task_id}/output",
            self._handle_background_task_output,
            methods=["GET"],
            tag="tasks",
            summary="Read a background task's output",
            response_model=BackgroundTaskOutputBody,
            error_statuses=(404,),
        )
        route(
            "/api/tasks/{task_id}/cancel",
            self._handle_cancel_background_task,
            methods=["POST"],
            tag="tasks",
            summary="Cancel a background task",
            response_model=BackgroundTaskStateBody,
            error_statuses=(404,),
        )
        route(
            "/api/tasks/{task_id}/retry",
            self._handle_retry_background_task,
            methods=["POST"],
            tag="tasks",
            summary="Retry a terminal background task",
            response_model=BackgroundTaskRetryBody,
            status_code=201,
            error_statuses=(400, 404),
        )
        route(
            "/api/tasks/{task_id}/steer",
            self._handle_steer_background_task,
            methods=["POST"],
            tag="tasks",
            summary="Steer a keep-alive background task",
            response_model=BackgroundTaskSteerBody,
            error_statuses=(400, 404),
        )
        route(
            "/api/sessions/{session_id}",
            self._handle_session_replay,
            methods=["GET"],
            tag="sessions",
            summary="Replay a persisted session (read-only)",
            response_model=RuntimeResponseBody,
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/events",
            self._handle_session_events,
            methods=["GET"],
            tag="sessions",
            summary="Stream a session's ordered events as SSE",
            streaming_frame=SessionEventFrameBody,
            error_statuses=(400, 404),
        )
        route(
            "/api/sessions/{session_id}/tasks",
            self._handle_list_background_tasks_by_parent_session,
            methods=["GET"],
            tag="sessions",
            summary="List a parent session's background tasks",
            response_model=list[BackgroundTaskSummaryBody],
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/delegated-context",
            self._handle_child_session_context,
            methods=["GET"],
            tag="sessions",
            summary="Read a delegated child session's context",
            response_model=BackgroundTaskOutputBody,
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/approval",
            self._handle_approval_resolution,
            methods=["POST"],
            tag="sessions",
            summary="Resolve a pending approval and continue the run",
            response_model=RuntimeResponseBody,
            error_statuses=(400, 404, 409),
        )
        route(
            "/api/sessions/{session_id}/question",
            self._handle_question_answer,
            methods=["POST"],
            tag="sessions",
            summary="Answer a pending question and continue the run",
            response_model=RuntimeResponseBody,
            error_statuses=(400, 404, 409),
        )
        route(
            "/api/sessions/{session_id}/result",
            self._handle_session_result,
            methods=["GET"],
            tag="sessions",
            summary="Read a session's terminal result",
            response_model=SessionResultBody,
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/debug",
            self._handle_session_debug,
            methods=["GET"],
            tag="sessions",
            summary="Read a session's debug snapshot",
            response_model=SessionDebugBody,
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/undo",
            self._handle_session_undo,
            methods=["POST"],
            tag="sessions",
            summary="Undo the session revert",
            response_model=SessionRevertBody,
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/revert",
            self._handle_session_revert,
            methods=["POST"],
            tag="sessions",
            summary="Write a session revert marker",
            response_model=SessionRevertBody,
            error_statuses=(400, 404),
        )
        route(
            "/api/sessions/{session_id}/unrevert",
            self._handle_session_unrevert,
            methods=["POST"],
            tag="sessions",
            summary="Clear the session revert marker",
            response_model=SessionRevertBody,
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/cancel",
            self._handle_cancel_session,
            methods=["POST"],
            tag="sessions",
            summary="Cancel the session's active run",
            response_model=SessionCancelBody,
            error_statuses=(400, 404),
        )
        route(
            "/api/sessions/{session_id}/resume",
            self._handle_resume,
            methods=["POST"],
            tag="sessions",
            summary="Explicitly resume an interrupted session",
            response_model=RuntimeResponseBody,
            error_statuses=(404,),
        )
        route(
            "/api/sessions/{session_id}/steer",
            self._handle_steer_session,
            methods=["POST"],
            tag="sessions",
            summary="Queue a steering message for the session",
            response_model=SessionSteerBody,
            error_statuses=(400, 404, 409),
        )
        route(
            "/api/providers/{provider_name}/models",
            self._handle_provider_models,
            methods=["GET"],
            tag="providers",
            summary="List a provider's models",
            response_model=ProviderModelsBody,
            alternate_statuses=(409,),
        )
        route(
            "/api/providers/{provider_name}/inspect",
            self._handle_provider_inspect,
            methods=["GET"],
            tag="providers",
            summary="Inspect a provider's endpoint and configuration",
            response_model=ProviderInspectBody,
            error_statuses=(400,),
            alternate_statuses=(409,),
        )
        route(
            "/api/providers/{provider_name}/validate",
            self._handle_provider_validation,
            methods=["POST"],
            tag="providers",
            summary="Validate a provider's credentials",
            response_model=ProviderValidationBody,
            error_statuses=(400,),
            alternate_statuses=(409,),
        )

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _close_runtime(
        runtime: RuntimeTransport,
        *,
        workspace_coordinator: WorkspaceRuntimeCoordinator | None = None,
    ) -> None:
        if workspace_coordinator is not None and workspace_coordinator.owns_runtime(runtime):
            return
        runtime.__exit__(None, None, None)

    @contextmanager
    def _active_request_scope(self) -> Iterator[None]:
        request_scope = self._workspace_coordinator.active_request() if self._workspace_coordinator is not None else nullcontext()
        with request_scope:
            yield

    @contextmanager
    def _runtime_lease(self) -> Iterator[RuntimeTransport]:
        """One request's runtime, owned for as long as the response lives.

        The coordinator's active-request slot and the runtime instance are held
        together: a request-scoped runtime is closed on the way out, while a
        coordinator-owned one survives.
        """
        with self._active_request_scope():
            runtime = self._runtime_factory()
            try:
                yield runtime
            finally:
                self._close_runtime(runtime, workspace_coordinator=self._workspace_coordinator)

    @staticmethod
    def _validated_id(value: str, *, field_name: str) -> str:
        try:
            validate_id(value, field_name=field_name)
        except ValueError:
            raise HttpError(404, "not found") from None
        return value

    @classmethod
    def _validated_session_id(cls, session_id: str) -> str:
        return cls._validated_id(session_id, field_name="session_id")

    @classmethod
    def _validated_task_id(cls, task_id: str) -> str:
        return cls._validated_id(task_id, field_name="task_id")

    # ------------------------------------------------------------------- streaming

    async def _stream_runtime_chunks(
        self,
        runtime: RuntimeTransport,
        request: RuntimeRequest,
    ) -> AsyncGenerator[RuntimeStreamChunk]:
        """Bridge the runtime's blocking stream generator onto the event loop.

        ``run_stream`` is synchronous and must not run on the loop, so a worker
        thread drives it and posts chunks back through an ``asyncio.Queue``.
        Posting is done from the loop's own thread-safe callback rather than
        through ``asyncio.to_thread``, which keeps every live stream off the
        shared default executor the resume/approval/question calls also use.

        When the response goes away the worker is told to stop, so a dropped
        client cannot leave a thread draining a run nobody is reading. The
        runtime's stream has no cancellation seam, so a chunk already in flight
        still completes; the stop lands at the next chunk boundary.
        """
        loop = asyncio.get_running_loop()
        chunk_queue: asyncio.Queue[RuntimeStreamChunk | Exception | None] = asyncio.Queue()
        stop_event = threading.Event()

        def _deliver(item: RuntimeStreamChunk | Exception | None) -> bool:
            try:
                loop.call_soon_threadsafe(chunk_queue.put_nowait, item)
            except RuntimeError:
                # The loop is gone: nothing can consume this stream any more.
                return False
            return True

        def _produce() -> None:
            try:
                for chunk in runtime.run_stream(request):
                    if stop_event.is_set():
                        break
                    if not _deliver(chunk):
                        break
            except Exception as exc:
                _deliver(exc)
            finally:
                _deliver(None)

        worker = threading.Thread(target=_produce, name="runtime-stream-worker", daemon=True)
        worker.start()
        try:
            while True:
                item = await chunk_queue.get()
                if item is None:
                    # ``None`` closes the queue: the worker has stopped producing.
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop_event.set()
            worker.join(timeout=_STREAM_WORKER_JOIN_SECONDS)

    async def _run_stream_frames(
        self,
        stream: AsyncGenerator[RuntimeStreamChunk],
        first_chunk: RuntimeStreamChunk,
        *,
        completion: StreamCompletion,
        show_thinking: bool,
    ) -> AsyncGenerator[bytes]:
        """Frames for ``POST /api/runtime/run/stream``.

        The first chunk is pulled by the endpoint before the response starts so
        that a pre-stream failure is still a JSON error; everything after it is
        streamed here. ``completion`` records whether the last frame was reached
        so the response can tell a finished stream from a dropped client.
        """
        session_emitter = _SessionStateEmitter()
        emitted_failed_chunk = False
        try:
            emitted_failed_chunk = _chunk_reports_failure(first_chunk)
            yield self._runtime_chunk_frame(first_chunk, session_emitter=session_emitter, show_thinking=show_thinking)
            async for chunk in stream:
                emitted_failed_chunk = _chunk_reports_failure(chunk) or emitted_failed_chunk
                yield self._runtime_chunk_frame(chunk, session_emitter=session_emitter, show_thinking=show_thinking)
            completion.finished = True
        except Exception:
            if not emitted_failed_chunk:
                logger.exception("unexpected transport streaming failure")
            # A failing runtime is not a dropped client: the client stays
            # connected to a stream that ended, so the run is not cancelled.
            completion.finished = True
        finally:
            await _aclose_async_iterator(stream)

    def _runtime_chunk_frame(
        self,
        chunk: RuntimeStreamChunk,
        *,
        session_emitter: _SessionStateEmitter,
        show_thinking: bool,
    ) -> bytes:
        return sse_frame(
            self._serialize_runtime_stream_chunk(
                chunk,
                session=session_emitter.serialize(chunk.session),
                show_thinking=show_thinking,
            )
        )

    async def _session_event_frames(
        self,
        runtime: RuntimeTransport,
        *,
        session_id: str,
        replay: RuntimeResponse,
        after_sequence: int,
        follow: bool,
        show_thinking: bool,
    ) -> AsyncGenerator[bytes]:
        """Frames for ``GET /api/sessions/{id}/events``."""
        yield sse_frame(
            {
                "kind": "session",
                "session": _serialize_session_state(replay.session),
                "event": None,
                "output": None,
            }
        )

        cursor = after_sequence
        pending_events: tuple[EventEnvelope, ...] = replay.events
        session_status = replay.session.status
        while True:
            for event in pending_events:
                if event.sequence <= cursor:
                    continue
                yield sse_frame(
                    {
                        "kind": "event",
                        "session": None,
                        "event": self._serialize_event(event, show_thinking=show_thinking),
                        "output": None,
                    }
                )
                cursor = event.sequence
            if not follow or session_status in _SESSION_TERMINAL_STATUSES:
                return
            # Idle tick: read only the events past the cursor and the persisted
            # status. Re-replaying the whole transcript here ran a full-log scan
            # plus policy projection on the event loop once per second for every
            # open follow stream.
            await asyncio.sleep(_SESSION_EVENT_FOLLOW_POLL_SECONDS)
            batch = runtime.session_events_after(
                session_id=session_id,
                after_sequence=cursor,
            )
            pending_events = batch.events
            session_status = batch.status

    # ------------------------------------------------------------------- handlers

    async def _handle_run_stream(
        self,
        payload: _RunStreamRequestPayload,
        show_thinking: ShowThinkingQuery = False,
    ) -> Response:
        try:
            runtime_request = _runtime_request_from(payload)
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None

        lease = ExitStack()
        try:
            try:
                runtime = lease.enter_context(self._runtime_lease())
            except Exception:
                logger.exception("unexpected transport streaming failure")
                raise HttpError(500, "internal server error") from None
            stream = self._stream_runtime_chunks(runtime, runtime_request)
            try:
                first_chunk = await anext(stream)
            except StopAsyncIteration:
                logger.error("runtime stream emitted no chunks before response start")
                raise HttpError(500, "internal server error") from None
            except RuntimeRequestError as exc:
                raise HttpError(400, str(exc)) from None
            except Exception:
                logger.exception("unexpected transport streaming failure")
                raise HttpError(500, "internal server error") from None
            on_client_disconnect = _ClientDisconnectCancellation(runtime, first_chunk.session.session.id)
            completion = StreamCompletion()
            frames = self._run_stream_frames(
                stream,
                first_chunk,
                completion=completion,
                show_thinking=show_thinking,
            )
        except BaseException:
            lease.close()
            raise
        return EventStreamResponse(
            frames,
            completion=completion,
            on_client_disconnect=on_client_disconnect,
            on_close=lease.close,
        )

    async def _handle_session_events(
        self,
        session_id: str,
        after_sequence: AfterSequenceQuery = 0,
        follow: FollowQuery = False,
        show_thinking: ShowThinkingQuery = False,
    ) -> Response:
        session_id = self._validated_session_id(session_id)
        lease = ExitStack()
        try:
            runtime = lease.enter_context(self._runtime_lease())
            try:
                replay = runtime.replay_session(session_id=session_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
            except Exception as exc:
                logger.exception("session event replay failed for %s", session_id)
                raise HttpError(500, f"session event replay failed: {exc}") from None
            frames = self._session_event_frames(
                runtime,
                session_id=session_id,
                replay=replay,
                after_sequence=after_sequence,
                follow=follow,
                show_thinking=show_thinking,
            )
        except BaseException:
            lease.close()
            raise
        return EventStreamResponse(frames, on_close=lease.close)

    async def _handle_openapi_document(self) -> Response:
        """Serve the API surface document the transport itself renders."""
        return json_response(self.openapi())

    async def _handle_list_sessions(self) -> Response:
        with self._runtime_lease() as runtime:
            # The flat session list is the main-session surface: delegated child
            # sessions belong only to the child-session view and are reachable
            # through the task/delegated-context endpoints, so exclude them here.
            top_level = [item for item in runtime.list_sessions() if item.session.parent_id is None]
            # Display-only fork depth comes from the runtime's forest projection
            # -- the same one the CLI tree and TUI picker render -- so no client
            # re-derives depth from provenance. The forest is a superset of this
            # filtered list: a row it omits serializes a null depth, never raises.
            depths = {entry.session_id: entry.depth for entry in runtime.session_forest()}
            payload = [self._serialize_stored_session_summary(item, depth=depths.get(item.session.id)) for item in top_level]
        return json_response(payload)

    async def _handle_start_background_task(self, payload: _RunStreamRequestPayload) -> Response:
        try:
            runtime_request = _runtime_request_from(payload)
        except ValueError as exc:
            raise HttpError(400, str(exc)) from None

        with self._runtime_lease() as runtime:
            try:
                task = runtime.start_background_task(runtime_request)
            except RuntimeRequestError as exc:
                raise HttpError(400, str(exc)) from None
        return json_response(self._serialize_background_task_state(task), status=201)

    async def _handle_list_background_tasks(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = [self._serialize_background_task_summary(item) for item in runtime.list_background_tasks()]
        return json_response(payload)

    async def _handle_list_background_tasks_by_parent_session(self, session_id: str) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            payload = [
                self._serialize_background_task_summary(item)
                for item in runtime.list_background_tasks_by_parent_session(parent_session_id=session_id)
            ]
        return json_response(payload)

    async def _handle_background_task_status(self, task_id: str) -> Response:
        task_id = self._validated_task_id(task_id)
        with self._runtime_lease() as runtime:
            try:
                task = runtime.load_background_task(task_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response(self._serialize_background_task_state(task))

    async def _handle_background_task_output(self, task_id: str, show_thinking: ShowThinkingQuery = False) -> Response:
        task_id = self._validated_task_id(task_id)
        with self._runtime_lease() as runtime:
            try:
                task_result = runtime.load_background_task_result(task_id)
                child_session_result: RuntimeSessionResult | None = None
                if task_result.child_session_id is not None:
                    try:
                        child_session_result = runtime.session_result(session_id=task_result.child_session_id)
                    except UnknownSessionError:
                        child_session_result = None
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response(
            {
                "task": self._serialize_background_task_result(task_result),
                "session_result": self._serialize_session_result(
                    child_session_result,
                    show_thinking=show_thinking,
                )
                if child_session_result is not None
                else None,
                "output": _resolved_task_output(task_result, child_session_result),
            }
        )

    async def _handle_child_session_context(self, session_id: str, show_thinking: ShowThinkingQuery = False) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            task_result = runtime.load_background_task_result_by_child_session(
                child_session_id=session_id,
            )
            if task_result is None:
                # The miss is a routing signal for clients (ordinary session
                # replay vs. delegated child context), so it carries a stable
                # code instead of leaving them to match on the message.
                raise HttpError(
                    404,
                    f"no delegated child context for session: {session_id}",
                    code="delegated_context_missing",
                )
            child_session_result: RuntimeSessionResult | None = None
            if task_result.child_session_id is not None:
                try:
                    child_session_result = runtime.session_result(
                        session_id=task_result.child_session_id,
                    )
                except UnknownSessionError:
                    child_session_result = None

        return json_response(
            {
                "task": self._serialize_background_task_result(task_result),
                "session_result": self._serialize_session_result(
                    child_session_result,
                    show_thinking=show_thinking,
                )
                if child_session_result is not None
                else None,
                "output": _resolved_task_output(task_result, child_session_result),
            }
        )

    async def _handle_cancel_background_task(self, task_id: str) -> Response:
        task_id = self._validated_task_id(task_id)
        with self._runtime_lease() as runtime:
            try:
                task = runtime.cancel_background_task(task_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response(self._serialize_background_task_state(task))

    async def _handle_retry_background_task(self, task_id: str) -> Response:
        task_id = self._validated_task_id(task_id)
        with self._runtime_lease() as runtime:
            try:
                task = runtime.retry_background_task(task_id)
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        return json_response(
            {
                "retry_of_task_id": task_id,
                "task": self._serialize_background_task_state(task),
            },
            status=201,
        )

    async def _handle_steer_background_task(self, task_id: str, payload: _TaskSteerRequestPayload) -> Response:
        task_id = self._validated_task_id(task_id)
        prompt = payload.prompt
        with self._runtime_lease() as runtime:
            try:
                task = runtime.steer_background_task(task_id, prompt)
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        return json_response(
            {
                "steer_prompt": prompt,
                "task": self._serialize_background_task_state(task),
            }
        )

    async def _handle_cancel_session(self, session_id: str, payload: _SessionCancelRequestPayload | None = None) -> Response:
        session_id = self._validated_session_id(session_id)
        cancel_request = payload if payload is not None else _SessionCancelRequestPayload()
        with self._runtime_lease() as runtime:
            result = runtime.cancel_session(
                session_id,
                run_id=cancel_request.run_id,
                reason=cancel_request.reason,
            )
        return json_response(result.as_payload())

    async def _handle_get_settings(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = runtime.web_settings()
        return json_response(payload)

    async def _handle_list_workspaces(self) -> Response:
        if self._workspace_coordinator is None:
            raise HttpError(404, "not found")
        return json_response(self._serialize_workspace_registry_snapshot(self._workspace_coordinator.snapshot()))

    async def _handle_open_workspace(self, payload: _WorkspaceOpenRequestPayload) -> Response:
        if self._workspace_coordinator is None:
            raise HttpError(404, "not found")
        try:
            snapshot = self._workspace_coordinator.open_workspace(payload.path)
        except WorkspaceOpenError as exc:
            raise HttpError(exc.status_code, str(exc), code=error_code(exc)) from None
        return json_response(self._serialize_workspace_registry_snapshot(snapshot))

    async def _handle_list_providers(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = [self._serialize_provider_summary(provider) for provider in runtime.list_provider_summaries()]
        return json_response(payload)

    async def _handle_provider_models(self, provider_name: str) -> Response:
        with self._runtime_lease() as runtime:
            result = runtime.provider_models_result(provider_name)
        status = 200 if result.configured else 409
        return json_response(self._serialize_provider_models_result(result), status=status)

    async def _handle_provider_inspect(self, provider_name: str) -> Response:
        with self._runtime_lease() as runtime:
            try:
                result = runtime.inspect_provider(provider_name)
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        status = 200 if result.summary.configured else 409
        return json_response(self._serialize_provider_inspect_result(result), status=status)

    async def _handle_provider_validation(self, provider_name: str) -> Response:
        with self._runtime_lease() as runtime:
            try:
                result = runtime.validate_provider_credentials(provider_name)
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        status = 200 if result.ok else 409
        return json_response(self._serialize_provider_validation_result(result), status=status)

    async def _handle_list_agents(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = [self._serialize_agent_summary(agent) for agent in runtime.list_agent_summaries()]
        return json_response(payload)

    async def _handle_list_skills(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = [self._serialize_skill_summary(skill) for skill in runtime.list_skill_summaries()]
        return json_response(payload)

    async def _handle_list_commands(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = [self._serialize_command_summary(command) for command in runtime.list_command_summaries()]
        return json_response(payload)

    async def _handle_get_status(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = self._serialize_runtime_status_snapshot(runtime.current_status())
        return json_response(payload)

    async def _handle_retry_mcp(self) -> Response:
        with self._runtime_lease() as runtime:
            try:
                payload = self._serialize_runtime_status_snapshot(runtime.retry_mcp_connections())
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        return json_response(payload)

    async def _handle_get_review(self) -> Response:
        with self._runtime_lease() as runtime:
            payload = self._serialize_workspace_review_snapshot(runtime.review_snapshot())
        return json_response(payload)

    async def _handle_get_review_diff(self, path: str) -> Response:
        if not path:
            raise HttpError(404, "not found")
        with self._runtime_lease() as runtime:
            try:
                payload = self._serialize_review_file_diff(runtime.review_diff(path))
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        return json_response(payload)

    async def _handle_update_settings(self, payload: _SettingsRequestPayload) -> Response:
        with self._runtime_lease() as runtime:
            try:
                result = runtime.update_web_settings(
                    provider=payload.provider,
                    provider_api_key=payload.provider_api_key,
                    model=payload.model,
                )
            except ValueError as exc:
                raise HttpError(400, str(exc)) from None
        return json_response(result)

    def _resume_session(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        # Keep runtime ownership in the worker even if the HTTP request disconnects.
        with self._runtime_lease() as runtime:
            if approval_request_id is None and approval_decision is None:
                return runtime.resume(session_id)
            return runtime.resume(
                session_id,
                approval_request_id=approval_request_id,
                approval_decision=approval_decision,
            )

    def _answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> RuntimeResponse:
        with self._runtime_lease() as runtime:
            return runtime.answer_question(
                session_id,
                question_request_id=question_request_id,
                responses=responses,
            )

    async def _handle_resume(self, session_id: str, show_thinking: ShowThinkingQuery = False) -> Response:
        session_id = self._validated_session_id(session_id)
        try:
            response = await asyncio.to_thread(self._resume_session, session_id)
        except ValueError as exc:
            raise HttpError(404, str(exc)) from None
        return json_response(self._serialize_runtime_response(response, show_thinking=show_thinking))

    async def _handle_session_replay(self, session_id: str, show_thinking: ShowThinkingQuery = False) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            try:
                response = runtime.replay_session(session_id=session_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response(self._serialize_runtime_response(response, show_thinking=show_thinking))

    async def _handle_session_result(self, session_id: str, show_thinking: ShowThinkingQuery = False) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            try:
                result = runtime.session_result(session_id=session_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response(self._serialize_session_result(result, show_thinking=show_thinking))

    async def _handle_session_debug(self, session_id: str, show_thinking: ShowThinkingQuery = False) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            try:
                snapshot = runtime.session_debug_snapshot(session_id=session_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response(
            serialize_session_debug_snapshot(
                snapshot,
                show_thinking=show_thinking,
            )
        )

    async def _handle_session_undo(self, session_id: str) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            try:
                marker = runtime.undo_session(session_id=session_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response({"revert_marker": serialize_revert_marker(marker)})

    async def _handle_session_revert(self, session_id: str, payload: _SessionRevertRequestPayload) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            try:
                marker = runtime.revert_session(
                    session_id=session_id,
                    sequence=payload.sequence,
                )
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response({"revert_marker": serialize_revert_marker(marker)})

    async def _handle_steer_session(self, session_id: str, payload: _SteerSessionRequestPayload) -> Response:
        session_id = self._validated_session_id(session_id)
        content = payload.content
        with self._runtime_lease() as runtime:
            try:
                queued = runtime.queue_steering(
                    session_id=session_id,
                    content=content,
                )
            except SessionSealedError as exc:
                raise HttpError(409, str(exc), code=error_code(exc)) from None
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response({"session_id": session_id, "queued": len(queued)})

    async def _handle_session_unrevert(self, session_id: str) -> Response:
        session_id = self._validated_session_id(session_id)
        with self._runtime_lease() as runtime:
            try:
                marker = runtime.unrevert_session(session_id=session_id)
            except ValueError as exc:
                raise HttpError(404, str(exc)) from None
        return json_response({"revert_marker": serialize_revert_marker(marker)})

    async def _handle_approval_resolution(
        self,
        session_id: str,
        payload: _ApprovalResolutionRequestPayload,
        show_thinking: ShowThinkingQuery = False,
    ) -> Response:
        session_id = self._validated_session_id(session_id)
        try:
            response = await asyncio.to_thread(
                self._resume_session,
                session_id,
                approval_request_id=payload.request_id,
                approval_decision=payload.decision,
            )
        except ValueError as exc:
            raise HttpError(409, str(exc), code=error_code(exc)) from None
        return json_response(self._serialize_runtime_response(response, show_thinking=show_thinking))

    async def _handle_question_answer(
        self,
        session_id: str,
        payload: _QuestionAnswerRequestPayload,
        show_thinking: ShowThinkingQuery = False,
    ) -> Response:
        session_id = self._validated_session_id(session_id)
        responses = tuple(
            QuestionResponse(
                header=item.header,
                answers=item.answers,
            )
            for item in (payload.responses if payload.responses is not None else ())
        )
        try:
            response = await asyncio.to_thread(
                self._answer_question,
                session_id,
                question_request_id=payload.request_id,
                responses=responses,
            )
        # 409 separates "nothing pending" from this route's 404 session errors (unknown session / mismatched request id).
        except NoPendingQuestionError as exc:
            raise HttpError(409, str(exc), code=error_code(exc)) from None
        except ValueError as exc:
            raise HttpError(404, str(exc), code=error_code(exc)) from None
        return json_response(self._serialize_runtime_response(response, show_thinking=show_thinking))

    # --------------------------------------------------------------------- static

    @staticmethod
    def _content_type_for_suffix(suffix: str) -> str:
        _CONTENT_TYPES: dict[str, str] = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".ttf": "font/ttf",
            ".txt": "text/plain; charset=utf-8",
            ".map": "application/octet-stream",
            ".webp": "image/webp",
            ".wasm": "application/wasm",
        }
        return _CONTENT_TYPES.get(suffix.lower(), "application/octet-stream")

    async def _serve_unmatched_path(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Last-resort handler for paths no API route claimed.

        Unknown ``/api`` paths stay JSON 404s; everything else is served from the
        frontend dist, with the SPA fallback for client-side routes.
        """
        path = cast(str, scope.get("path", "/"))
        if is_api_path(path):
            raise HttpError(404, "not found")
        response = self._static_file_response(path, cast(str, scope.get("method", "GET")))
        await response(scope, receive, send)

    def _static_file_response(self, path: str, method: str) -> Response:
        if self._frontend_dist is None:
            raise HttpError(404, "not found")

        normalized_path = path.lstrip("/")
        if not normalized_path:
            normalized_path = "index.html"

        file_path = self._frontend_dist / normalized_path

        # Prevent directory traversal
        try:
            resolved = file_path.resolve()
            resolved.relative_to(self._frontend_dist.resolve())
        except ValueError:
            raise HttpError(404, "not found") from None

        if resolved.is_file():
            content_type = self._content_type_for_suffix(resolved.suffix)
            return Response(resolved.read_bytes(), media_type=content_type)

        # SPA fallback is only for route-like paths. Missing assets should
        # stay 404 so browsers do not try to parse index.html as JS/CSS/etc.
        if resolved.suffix:
            raise HttpError(404, "not found")

        # SPA fallback — serve index.html for client-side routing, which only
        # ever happens for a navigation (GET/HEAD); a write to an unknown path
        # must not be answered with a rendered page.
        if method not in ("GET", "HEAD"):
            raise HttpError(404, "not found")
        index_path = (self._frontend_dist / "index.html").resolve()
        if index_path.is_file():
            return Response(index_path.read_bytes(), media_type="text/html; charset=utf-8")

        raise HttpError(404, "not found")

    @staticmethod
    def _serialize_runtime_stream_chunk(
        chunk: RuntimeStreamChunk,
        *,
        session: dict[str, object] | None,
        show_thinking: bool = False,
    ) -> dict[str, object]:
        event = chunk.event
        return {
            "kind": chunk.kind,
            "session": session,
            "event": None if event is None else RuntimeTransportApp._serialize_event(event, show_thinking=show_thinking),
            "output": chunk.output,
        }

    @staticmethod
    def _serialize_runtime_response(
        response: RuntimeResponse,
        *,
        show_thinking: bool = False,
    ) -> dict[str, object]:
        return {
            "session": _serialize_session_state(response.session),
            "events": [RuntimeTransportApp._serialize_event(event, show_thinking=show_thinking) for event in response.events],
            "output": response.output,
        }

    @staticmethod
    def _serialize_stored_session_summary(summary: StoredSessionSummary, *, depth: int | None) -> dict[str, object]:
        return {
            "session": _serialize_session_ref(summary.session),
            "status": summary.status,
            "turn": summary.turn,
            "prompt": summary.prompt,
            "updated_at": summary.updated_at,
            "title": summary.title,
            "depth": depth,
        }

    @staticmethod
    def _serialize_background_task_request_snapshot(
        request: BackgroundTaskRequestSnapshot,
    ) -> dict[str, object]:
        return {
            "prompt": request.prompt,
            "session_id": request.session_id,
            "parent_session_id": request.parent_session_id,
            "metadata": request.metadata,
            "allocate_session_id": request.allocate_session_id,
        }

    @staticmethod
    def _serialize_subagent_routing(
        routing: SubagentRoutingIdentity | None,
    ) -> dict[str, object] | None:
        if routing is None:
            return None
        payload: dict[str, object] = {"mode": routing.mode}
        if routing.subagent_type is not None:
            payload["subagent_type"] = routing.subagent_type
        if routing.description is not None:
            payload["description"] = routing.description
        if routing.command is not None:
            payload["command"] = routing.command
        return payload

    @staticmethod
    def _serialize_background_task_state(task: BackgroundTaskState) -> dict[str, object]:
        return {
            "task": {"id": task.task.id},
            "status": task.status,
            "request": RuntimeTransportApp._serialize_background_task_request_snapshot(task.request),
            "parent_session_id": task.parent_session_id,
            "requested_child_session_id": task.request.session_id,
            "child_session_id": task.child_session_id,
            "approval_request_id": task.approval_request_id,
            "question_request_id": task.question_request_id,
            "result_available": task.result_available,
            "cancellation_cause": task.cancellation_cause,
            "error": task.error,
            "created_at": task.created_at,
            "created_at_unix_ms": task.created_at_unix_ms,
            "updated_at": task.updated_at,
            "started_at": task.started_at,
            "started_at_unix_ms": task.started_at_unix_ms,
            "finished_at": task.finished_at,
            "finished_at_unix_ms": task.finished_at_unix_ms,
            "cancel_requested_at": task.cancel_requested_at,
            "keep_alive": task.keep_alive,
            "steer_prompt": task.steer_prompt,
            "routing": RuntimeTransportApp._serialize_subagent_routing(task.routing_identity),
            "observability": (None if task.observability is None else task.observability.as_payload()),
            "output_schema": task.output_schema,
            "schema_mode": task.schema_mode,
            "structured_output": task.structured_output,
            "schema_validation": (None if task.schema_validation is None else task.schema_validation.as_payload()),
        }

    @staticmethod
    def _serialize_background_task_summary(task: StoredBackgroundTaskSummary) -> dict[str, object]:
        return {
            "task": {"id": task.task.id},
            "status": task.status,
            "prompt": task.prompt,
            "session_id": task.session_id,
            "error": task.error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "created_at_unix_ms": task.created_at_unix_ms,
            "keep_alive": task.keep_alive,
            "steer_prompt": task.steer_prompt,
            "output_schema": task.output_schema,
            "schema_mode": task.schema_mode,
            "observability": (None if task.observability is None else task.observability.as_payload()),
        }

    @staticmethod
    def _serialize_session_result(
        result: RuntimeSessionResult,
        *,
        show_thinking: bool = False,
    ) -> dict[str, object]:
        return {
            "session": _serialize_session_state(result.session),
            "prompt": result.prompt,
            "status": result.status,
            "summary": result.summary,
            "output": result.output,
            "error": result.error,
            "last_event_sequence": result.last_event_sequence,
            "revert_marker": serialize_revert_marker(result.revert_marker),
            "title": result.title,
            "transcript": [
                {
                    **RuntimeTransportApp._serialize_event(event, show_thinking=show_thinking),
                    "reverted": result.revert_marker is not None and result.revert_marker.active and event.sequence >= result.revert_marker.sequence,
                }
                for event in result.transcript
            ],
        }

    @staticmethod
    def _serialize_background_task_result(result: BackgroundTaskResult) -> dict[str, object]:
        return {
            "task_id": result.task_id,
            "status": result.status,
            "parent_session_id": result.parent_session_id,
            "requested_child_session_id": result.requested_child_session_id,
            "delegated_prompt": result.delegated_prompt,
            "child_session_id": result.child_session_id,
            "approval_request_id": result.approval_request_id,
            "question_request_id": result.question_request_id,
            "approval_blocked": result.approval_blocked,
            "summary_output": result.summary_output,
            "error": result.error,
            "result_available": result.result_available,
            "cancellation_cause": result.cancellation_cause,
            "duration_seconds": result.duration_seconds,
            "tool_call_count": result.tool_call_count,
            "routing": RuntimeTransportApp._serialize_subagent_routing(result.routing),
            "observability": (None if result.observability is None else result.observability.as_payload()),
            "hook_reminder": result.hook_reminder,
            "delegation": result.delegated_execution.as_payload(),
            "message": result.delegated_message.as_payload(),
            "structured_output": result.structured_output,
            "schema_validation": (None if result.schema_validation is None else result.schema_validation.as_payload()),
        }

    @staticmethod
    def _serialize_workspace_summary(summary: WorkspaceSummary) -> dict[str, object]:
        return {
            "path": summary.path,
            "label": summary.label,
            "available": summary.available,
            "current": summary.current,
            "last_opened_at": summary.last_opened_at,
        }

    @staticmethod
    def _serialize_workspace_registry_snapshot(
        snapshot: WorkspaceRegistrySnapshot,
    ) -> dict[str, object]:
        return {
            "current": (None if snapshot.current is None else RuntimeTransportApp._serialize_workspace_summary(snapshot.current)),
            "recent": [RuntimeTransportApp._serialize_workspace_summary(item) for item in snapshot.recent],
            "candidates": [RuntimeTransportApp._serialize_workspace_summary(item) for item in snapshot.candidates],
        }

    @staticmethod
    def _serialize_provider_summary(summary: ProviderSummary) -> dict[str, object]:
        return {
            "name": summary.name,
            "label": summary.label,
            "configured": summary.configured,
            "current": summary.current,
        }

    @staticmethod
    def _serialize_provider_model_metadata(metadata: ProviderModelMetadata) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "context_window": metadata.context_window,
                "max_input_tokens": metadata.max_input_tokens,
                "max_output_tokens": metadata.max_output_tokens,
                "supports_tools": metadata.supports_tools,
                "supports_vision": metadata.supports_vision,
                "supports_streaming": metadata.supports_streaming,
                "supports_reasoning": metadata.supports_reasoning,
                "supports_json_mode": metadata.supports_json_mode,
                "cost_per_input_token": metadata.cost_per_input_token,
                "cost_per_output_token": metadata.cost_per_output_token,
                "cost_per_cache_read_token": metadata.cost_per_cache_read_token,
                "cost_per_cache_write_token": metadata.cost_per_cache_write_token,
                "supports_reasoning_effort": metadata.supports_reasoning_effort,
                "default_reasoning_effort": metadata.default_reasoning_effort,
                "supported_effort_levels": list(metadata.supported_effort_levels) if metadata.supported_effort_levels is not None else None,
                "supports_reasoning_summary": metadata.supports_reasoning_summary,
                "supports_thinking_budget": metadata.supports_thinking_budget,
                "supports_interleaved_reasoning": metadata.supports_interleaved_reasoning,
                "reasoning_visibility": metadata.reasoning_visibility,
                "modalities_input": list(metadata.modalities_input) if metadata.modalities_input is not None else None,
                "modalities_output": list(metadata.modalities_output) if metadata.modalities_output is not None else None,
                "model_status": metadata.model_status,
                "tool_feedback_mode": metadata.tool_feedback_mode,
                "api": metadata.api,
                "display_name": metadata.display_name,
            }.items()
            if value is not None
        }

    @staticmethod
    def _serialize_provider_models_result(result: ProviderModelsResult) -> dict[str, object]:
        return {
            "provider": result.provider,
            "configured": result.configured,
            "models": list(result.models),
            "model_metadata": {
                model: RuntimeTransportApp._serialize_provider_model_metadata(metadata) for model, metadata in result.model_metadata.items()
            },
            "source": result.source,
            "last_refresh_status": result.last_refresh_status,
            "last_error": result.last_error,
            "discovery_mode": result.discovery_mode,
        }

    @staticmethod
    def _serialize_provider_inspect_result(result: ProviderInspectResult) -> dict[str, object]:
        return {
            "provider": RuntimeTransportApp._serialize_provider_summary(result.summary),
            "models": RuntimeTransportApp._serialize_provider_models_result(result.models),
            "validation": RuntimeTransportApp._serialize_provider_validation_result(result.validation),
            "current_model": result.current_model,
            "current_model_metadata": (
                None
                if result.current_model_metadata is None
                else RuntimeTransportApp._serialize_provider_model_metadata(result.current_model_metadata)
            ),
            "readiness": (None if result.readiness is None else RuntimeTransportApp._serialize_provider_readiness_result(result.readiness)),
        }

    @staticmethod
    def _serialize_provider_readiness_result(result: ProviderReadinessResult) -> dict[str, object]:
        return {
            "provider": result.provider,
            "model": result.model,
            "configured": result.configured,
            "ok": result.ok,
            "status": result.status,
            "guidance": result.guidance,
            "auth_present": result.auth_present,
            "streaming_configured": result.streaming_configured,
            "streaming_supported": result.streaming_supported,
            "context_window": result.context_window,
            "max_output_tokens": result.max_output_tokens,
            "fallback_chain": list(result.fallback_chain),
            "reasoning_controls": result.reasoning_controls,
        }

    @staticmethod
    def _serialize_provider_validation_result(
        result: ProviderValidationResult,
    ) -> dict[str, object]:
        return {
            "provider": result.provider,
            "configured": result.configured,
            "ok": result.ok,
            "status": result.status,
            "message": result.message,
            "source": result.source,
            "last_error": result.last_error,
            "discovery_mode": result.discovery_mode,
        }

    @staticmethod
    def _serialize_agent_summary(summary: AgentSummary) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": summary.id,
            "label": summary.label,
            "description": summary.description,
            "mode": summary.mode,
            "selectable": summary.selectable,
            "configured": summary.configured,
            "model": summary.model,
            "model_label": summary.model_label,
            "model_source": summary.model_source,
            "provider": summary.provider,
            "fallback_chain": list(summary.fallback_chain),
        }
        if summary.source_scope is not None:
            payload["source_scope"] = summary.source_scope
        if summary.source_path is not None:
            payload["source_path"] = summary.source_path
        return payload

    @staticmethod
    def _serialize_skill_summary(summary: SkillSummary) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": summary.name,
            "description": summary.description,
            "origin": summary.origin,
        }
        if summary.source_path is not None:
            payload["source_path"] = summary.source_path
        return payload

    @staticmethod
    def _serialize_command_summary(summary: CommandSummary) -> dict[str, object]:
        return {
            "name": summary.name,
            "description": summary.description,
            "source": summary.source,
            "enabled": summary.enabled,
            "hidden": summary.hidden,
            "agent": summary.agent,
            "model": summary.model,
            "subtask": summary.subtask,
            "path": summary.path,
        }

    @staticmethod
    def _serialize_git_status_snapshot(snapshot: GitStatusSnapshot) -> dict[str, object]:
        return {
            "state": snapshot.state,
            "root": snapshot.root,
            "branch": snapshot.branch,
            "error": snapshot.error,
        }

    @staticmethod
    def _serialize_capability_status_snapshot(
        snapshot: CapabilityStatusSnapshot,
    ) -> dict[str, object]:
        return {
            "state": snapshot.state,
            "error": snapshot.error,
            "details": snapshot.details,
        }

    @staticmethod
    def _serialize_runtime_status_snapshot(
        snapshot: RuntimeStatusSnapshot,
    ) -> dict[str, object]:
        return {
            "git": RuntimeTransportApp._serialize_git_status_snapshot(snapshot.git),
            "lsp": RuntimeTransportApp._serialize_capability_status_snapshot(snapshot.lsp),
            "mcp": RuntimeTransportApp._serialize_capability_status_snapshot(snapshot.mcp),
            "acp": RuntimeTransportApp._serialize_capability_status_snapshot(snapshot.acp),
            "background_tasks": {
                "active_worker_slots": snapshot.background_tasks.active_worker_slots,
                "queued_count": snapshot.background_tasks.queued_count,
                "running_count": snapshot.background_tasks.running_count,
                "terminal_count": snapshot.background_tasks.terminal_count,
                "default_concurrency": snapshot.background_tasks.default_concurrency,
                "provider_concurrency": snapshot.background_tasks.provider_concurrency,
                "model_concurrency": snapshot.background_tasks.model_concurrency,
                "status_counts": snapshot.background_tasks.status_counts,
            },
        }

    @staticmethod
    def _serialize_review_changed_file(item: ReviewChangedFile) -> dict[str, object]:
        return {
            "path": item.path,
            "change_type": item.change_type,
            "old_path": item.old_path,
        }

    @staticmethod
    def _serialize_review_tree_node(node: ReviewTreeNode) -> dict[str, object]:
        return {
            "path": node.path,
            "name": node.name,
            "kind": node.kind,
            "changed": node.changed,
            "children": [RuntimeTransportApp._serialize_review_tree_node(child) for child in node.children],
        }

    @staticmethod
    def _serialize_workspace_review_snapshot(
        snapshot: WorkspaceReviewSnapshot,
    ) -> dict[str, object]:
        return {
            "root": snapshot.root,
            "git": RuntimeTransportApp._serialize_git_status_snapshot(snapshot.git),
            "changed_files": [RuntimeTransportApp._serialize_review_changed_file(item) for item in snapshot.changed_files],
            "tree": [RuntimeTransportApp._serialize_review_tree_node(node) for node in snapshot.tree],
        }

    @staticmethod
    def _serialize_review_file_diff(diff: ReviewFileDiff) -> dict[str, object]:
        return {
            "root": diff.root,
            "path": diff.path,
            "state": diff.state,
            "diff": diff.diff,
        }

    @staticmethod
    def _serialize_event(
        event: EventEnvelope,
        *,
        show_thinking: bool = False,
    ) -> dict[str, object]:
        delegated = event.delegated_lifecycle
        payload: dict[str, object] = {
            "session_id": event.session_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "source": event.source,
            "payload": redact_reasoning_payload(
                event.event_type,
                event.payload,
                show_thinking=show_thinking,
            ),
        }
        if delegated is not None:
            payload["delegated_lifecycle"] = RuntimeTransportApp._serialize_delegated_lifecycle_event(delegated)
        return payload

    @staticmethod
    def _serialize_delegated_lifecycle_event(
        delegated: DelegatedLifecycleEventPayload,
    ) -> dict[str, object]:
        return delegated.as_payload()


def create_runtime_app(
    *,
    workspace: Path,
    config: RuntimeConfig | None = None,
    runtime_factory: Callable[[], RuntimeTransport] | None = None,
    frontend_dist: Path | None = None,
) -> RuntimeTransportApp:
    if runtime_factory is not None:
        return RuntimeTransportApp(runtime_factory=runtime_factory, frontend_dist=frontend_dist)

    resolved_workspace = workspace.resolve()
    if not resolved_workspace.is_dir():
        return RuntimeTransportApp(
            runtime_factory=lambda: _default_runtime_class()(workspace=resolved_workspace, config=config),
            frontend_dist=frontend_dist,
        )

    coordinator = WorkspaceRuntimeCoordinator[RuntimeTransport](
        initial_workspace=resolved_workspace,
        runtime_factory=lambda workspace: _default_runtime_class()(
            workspace=workspace,
            config=config,
        ),
        config=config,
    )

    def resolved_factory() -> RuntimeTransport:
        return coordinator.runtime()

    return RuntimeTransportApp(
        runtime_factory=resolved_factory,
        workspace_coordinator=coordinator,
        frontend_dist=frontend_dist,
    )
