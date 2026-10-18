"""Kernel HTTP transport: a small FastAPI app over one Kernel authority.

Routes are thin projections of ``voidcode.kernel.engine.Kernel`` methods.
Request bodies validate once at ingress (see ``models.py``); responses render
byte-stable JSON or hand-framed SSE. The app never invents commands, views,
or provider calls — errors from the kernel surface verbatim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from ...sdk.commands import CommandCatalog, CommandResult
from ...sdk.json_values import json_wire_object
from ...sdk.services import SessionUsageReport
from ...sdk.views import ViewsSnapshot
from ..bootstrap import open_kernel
from ..engine import Kernel, KernelResult
from ..storage.store import SessionRecord, StoredEvent
from .models import (
    CheckpointBody,
    ConfirmRequest,
    ContinuationsBody,
    CreateSessionRequest,
    DrainRequest,
    InvokeRequest,
    ResumeRequest,
    RunRequest,
    SessionsBody,
    ShowBody,
    StoredEventBody,
)

logger = logging.getLogger(__name__)

_FOLLOW_POLL_SECONDS = 1.0
_FOLLOW_TAIL_LIMIT = 100
_JSON_MEDIA_TYPE = "application/json; charset=utf-8"


class JsonResponse(JSONResponse):
    media_type = _JSON_MEDIA_TYPE

    def render(self, content: object) -> bytes:
        return json.dumps(content, sort_keys=True, allow_nan=False).encode("utf-8")


def _ok(payload: object, *, status: int = 200) -> JsonResponse:
    return JsonResponse(payload, status_code=status)


def _error(status: int, message: str, *, code: str | None = None) -> JsonResponse:
    return _ok({"error": message, "code": code}, status=status)


class HttpError(HTTPException):
    def __init__(self, status_code: int, message: str, *, code: str | None = None) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


async def _http_exception_handler(_request: Request, exc: HTTPException) -> JsonResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if detail == "Not Found":
        detail = "not found"
    elif detail == "Method Not Allowed":
        detail = "method not allowed"
    code = exc.code if isinstance(exc, HttpError) else None
    return _error(exc.status_code, detail, code=code)


async def _validation_error_handler(_request: Request, exc: RequestValidationError) -> JsonResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = tuple(first.get("loc", ()))
    loc = loc[1:] if loc and loc[0] in {"body", "query", "path", "header", "cookie"} else loc
    path = ".".join(str(item) for item in loc)
    error_type = str(first.get("type", ""))
    # A request that fails ingress validation never reaches a lookup or dispatch:
    # missing/blank required fields (e.g. expected_revision) fail here, visibly.
    if error_type == "json_invalid":
        return _error(400, "request body must be valid JSON", code="invalid_params")
    if error_type == "missing" and not path:
        return _error(400, "request body must be valid JSON", code="invalid_params")
    message = str(first.get("msg", "invalid request"))
    return _error(400, f"{path} {message}" if path else message, code="invalid_params")


async def _unhandled_handler(request: Request, exc: Exception) -> Response:
    logger.error("unhandled kernel transport failure on %s", request.scope.get("path", ""), exc_info=exc)
    return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")


def sse_frame(payload: object) -> bytes:
    return b"data: " + json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n\n"


class EventStreamResponse(StreamingResponse):
    media_type = "text/event-stream"

    def __init__(self, frames: AsyncGenerator[bytes]) -> None:
        super().__init__(frames, media_type="text/event-stream", headers={"cache-control": "no-cache"})


def _kernel_error(error: Exception) -> HttpError:
    message = str(error)
    # A client acting on a stale or missing displayed revision is refused before
    # any dispatch: the refusal is visible and never retried automatically.
    if "is stale; refresh the catalog" in message:
        return HttpError(409, message, code="stale_revision")
    if "expected_revision must be the revision the client displayed" in message:
        return HttpError(400, message, code="invalid_params")
    if isinstance(error, KeyError) or "unknown session" in message or "does not exist" in message:
        return HttpError(404, message)
    if isinstance(error, PermissionError):
        return HttpError(403, message)
    if isinstance(error, (ValueError, RuntimeError)):
        return HttpError(400, message)
    return HttpError(500, "internal server error")


def _validated_session_id(value: str) -> str:
    if not value or "/" in value or "\0" in value or len(value) > 128:
        raise HttpError(400, "session_id must be a non-empty string without '/' or NUL within 128 characters")
    return value


def _session_payload(record: SessionRecord) -> dict[str, object]:
    return {
        "session_id": record.session_id,
        "workspace": record.workspace,
        "status": record.status,
        "active_revision_id": record.active_revision_id,
        "run_id": record.run_id,
        "turn": record.turn,
        "last_event_sequence": record.last_event_sequence,
    }


def _event_payload(session_id: str, event: StoredEvent) -> dict[str, object]:
    return {
        "session_id": session_id,
        "sequence": event.sequence,
        "type": event.event_type,
        "run_id": event.run_id,
        "revision_id": event.revision_id,
        "source": event.source,
        "actor": event.actor,
        "payload": json_wire_object(event.payload),
    }


class KernelTransportApp(FastAPI):
    """The kernel's HTTP transport as a FastAPI application."""

    def __init__(
        self,
        *,
        kernel_factory: Callable[[], Kernel],
        bearer_token: str | None = None,
        allow_origin: str | None = None,
    ) -> None:
        super().__init__(
            title="VoidCode kernel API",
            description='Generic kernel transport. Every /api error uses the {"error", "code"} envelope.',
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
            redirect_slashes=False,
            default_response_class=JsonResponse,
            lifespan=self._lifespan,
            exception_handlers={
                HTTPException: _http_exception_handler,
                RequestValidationError: _validation_error_handler,
                Exception: _unhandled_handler,
            },
        )
        self._kernel_factory = kernel_factory
        self._kernel: Kernel | None = None
        self._bearer_token = bearer_token
        self._allow_origin = allow_origin
        self._register_routes()

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncGenerator[None]:
        kernel = await asyncio.to_thread(self._kernel_factory)
        self._kernel = kernel
        try:
            yield
        finally:
            self._kernel = None
            await asyncio.to_thread(kernel.close)

    def _kernel_or_503(self) -> Kernel:
        if self._kernel is None:
            raise HttpError(503, "kernel is not started")
        return self._kernel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "lifespan"):
            raise RuntimeError(f"unsupported scope type: {scope.get('type')!r}")
        if scope.get("type") == "http":
            path = str(scope.get("path", ""))
            if path == "/api" or path.startswith("/api/"):
                raw_headers = scope.get("headers", [])
                headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in raw_headers}
                origin = headers.get("origin")
                if scope.get("method") == "OPTIONS":
                    cors_resp = Response(
                        status_code=204,
                        headers={
                            "access-control-allow-origin": origin or "*",
                            "access-control-allow-methods": "GET, POST, OPTIONS",
                            "access-control-allow-headers": "Authorization, Content-Type, Origin, Accept",
                            "access-control-allow-credentials": "true",
                        },
                    )
                    await cors_resp(scope, receive, send)
                    return
                challenge = self._auth_challenge(scope)
                if challenge is not None:
                    await challenge(scope, receive, send)
                    return
                if origin:

                    async def cors_send(message: Any) -> None:
                        if message.get("type") == "http.response.start":
                            msg_headers = list(message.get("headers", []))
                            msg_headers.append((b"access-control-allow-origin", origin.encode("latin-1")))
                            msg_headers.append((b"access-control-allow-credentials", b"true"))
                            message["headers"] = msg_headers
                        await send(message)

                    await super().__call__(scope, receive, cors_send)
                    return
        await super().__call__(scope, receive, send)

    def _auth_challenge(self, scope: Scope) -> Response | None:
        headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in scope.get("headers", [])}
        if self._bearer_token is not None:
            presented = headers.get("authorization", "")
            scheme, _, credential = presented.partition(" ")
            if scheme.lower() != "bearer" or not credential or not secrets.compare_digest(credential, self._bearer_token):
                return _error(401, "missing or invalid bearer token", code="unauthorized")
        if self._allow_origin is not None:
            origin = headers.get("origin")
            if origin is not None and origin != self._allow_origin:
                return _error(403, "origin is not allowed", code="forbidden_origin")
        return None

    def openapi(self) -> dict[str, Any]:  # type: ignore[override]
        doc = super().openapi()
        schemas = doc.setdefault("components", {}).setdefault("schemas", {})
        from pydantic import TypeAdapter

        from ...sdk.views import (
            ChoicesViewData,
            ConfirmationViewData,
            DiffViewData,
            FieldsViewData,
            FormViewData,
            MarkdownViewData,
            NoticeViewData,
            StatusViewData,
        )

        view_classes = [
            NoticeViewData,
            MarkdownViewData,
            FieldsViewData,
            ChoicesViewData,
            ConfirmationViewData,
            FormViewData,
            DiffViewData,
            StatusViewData,
        ]
        for cls in view_classes:
            s = TypeAdapter(cls).json_schema(ref_template="#/components/schemas/{model}")
            defs = s.pop("$defs", None)
            if defs:
                for def_name, def_schema in defs.items():
                    schemas[def_name] = def_schema
            schemas[cls.__name__] = s

        schemas["PluginViewData"] = {"oneOf": [{"$ref": f"#/components/schemas/{cls.__name__}"} for cls in view_classes]}
        if "PluginViewDescriptor" in schemas and "properties" in schemas["PluginViewDescriptor"]:
            schemas["PluginViewDescriptor"]["properties"]["data"] = {"$ref": "#/components/schemas/PluginViewData"}
        return doc

    def _route(
        self,
        path: str,
        endpoint: Any,
        *,
        methods: list[str],
        summary: str,
        response_model: Any = None,
        status_code: int = 200,
        streaming_frame: type[BaseModel] | None = None,
        in_schema: bool = True,
    ) -> None:
        responses: dict[int | str, Any] = {
            400: {"description": "Invalid request"},
            401: {"description": "Missing or invalid bearer token"},
            403: {"description": "Forbidden"},
            404: {"description": "Not found"},
            500: {"description": "Internal server error"},
        }
        response_class: type[Response] = JsonResponse
        if streaming_frame is not None:
            responses[200] = {
                "description": "Server-sent event stream",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
            response_class = EventStreamResponse
        self.add_api_route(
            path,
            endpoint,
            methods=methods,
            tags=["kernel"],
            summary=summary,
            response_model=streaming_frame or response_model,
            status_code=status_code,
            responses=responses,
            response_class=response_class,
            include_in_schema=in_schema,
        )

    def _register_routes(self) -> None:
        route = self._route
        route("/api/openapi.json", self._handle_openapi_document, methods=["GET"], summary="Read this API as OpenAPI", in_schema=False)
        route("/api/commands", self._handle_command_catalog, methods=["GET"], summary="List admitted commands", response_model=CommandCatalog)
        route("/api/sessions", self._handle_list_sessions, methods=["GET"], summary="List sessions", response_model=SessionsBody)
        route(
            "/api/sessions", self._handle_create_session, methods=["POST"], summary="Create a session", response_model=SessionRecord, status_code=201
        )  # noqa: E501
        route("/api/sessions/{session_id}", self._handle_get_session, methods=["GET"], summary="Get a session", response_model=SessionRecord)
        route("/api/sessions/{session_id}/show", self._handle_show_session, methods=["GET"], summary="Show session state", response_model=ShowBody)
        route("/api/sessions/{session_id}/run", self._handle_run, methods=["POST"], summary="Run an agent turn", response_model=KernelResult)
        route("/api/sessions/{session_id}/resume", self._handle_resume, methods=["POST"], summary="Resume a session", response_model=KernelResult)
        route(
            "/api/sessions/{session_id}/invoke/{name}",
            self._handle_invoke,
            methods=["POST"],
            summary="Invoke a command",
            response_model=CommandResult,
        )
        route(
            "/api/sessions/{session_id}/confirm",
            self._handle_confirm,
            methods=["POST"],
            summary="Resolve a confirmation",
            response_model=CommandResult,
        )
        route("/api/sessions/{session_id}/views", self._handle_views, methods=["GET"], summary="Render plugin views", response_model=ViewsSnapshot)
        route(
            "/api/sessions/{session_id}/continuations",
            self._handle_drain,
            methods=["POST"],
            summary="Drain continuations",
            response_model=ContinuationsBody,
        )
        route(
            "/api/sessions/{session_id}/usage", self._handle_usage, methods=["GET"], summary="Read session usage", response_model=SessionUsageReport
        )  # noqa: E501
        route("/api/sessions/{session_id}/cancel", self._handle_cancel, methods=["POST"], summary="Request cancellation for one session")
        route("/api/sessions/{session_id}/events", self._handle_events, methods=["GET"], summary="Replay session events", streaming_frame=ShowBody)

    async def _handle_openapi_document(self) -> Response:
        return _ok(self.openapi())

    async def _handle_command_catalog(self) -> Response:
        kernel = self._kernel_or_503()
        try:
            catalog = await asyncio.to_thread(kernel.command_catalog)
        except Exception as exc:
            raise _kernel_error(exc) from None
        # One immutable snapshot: descriptors and the revision they were captured
        # from travel together, so a client can echo it verbatim later.
        return _ok(catalog.as_payload())

    async def _handle_list_sessions(self, limit: int = Query(default=100, ge=1, le=1000)) -> Response:
        kernel = self._kernel_or_503()
        try:
            records = await asyncio.to_thread(kernel.sessions, limit=limit)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok({"sessions": [_session_payload(record) for record in records]})

    async def _handle_create_session(self, payload: CreateSessionRequest | None = None) -> Response:
        kernel = self._kernel_or_503()
        requested = payload.session_id if payload is not None else None
        if requested is not None:
            _validated_session_id(requested)
        try:
            record = await asyncio.to_thread(kernel.create_session, requested)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(_session_payload(record), status=201)

    async def _handle_get_session(self, session_id: str) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        try:
            record = await asyncio.to_thread(kernel.session, session_id)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(_session_payload(record))

    async def _handle_show_session(self, session_id: str, events: bool = Query(default=False)) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        try:
            payload = await asyncio.to_thread(kernel.show, session_id, events=events)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(payload)

    async def _handle_run(self, session_id: str, payload: RunRequest) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        if payload.session_id is not None and payload.session_id != session_id:
            raise HttpError(400, "session_id in the path and body must match")
        try:
            result = await asyncio.to_thread(kernel.run, payload.prompt, session_id=session_id, max_steps=payload.max_steps)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_resume(self, session_id: str, payload: ResumeRequest | None = None) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        request = payload or ResumeRequest()
        try:
            result = await asyncio.to_thread(
                kernel.resume,
                session_id,
                approval_request_id=request.approval_request_id,
                decision=request.decision,
                max_steps=request.max_steps,
            )
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_invoke(self, session_id: str, name: str, payload: InvokeRequest) -> Response:
        _validated_session_id(session_id)
        if not name or "/" in name or "\0" in name or len(name) > 128:
            raise HttpError(400, "command name must be a non-empty string without '/' or NUL within 128 characters")
        kernel = self._kernel_or_503()
        try:
            result = await asyncio.to_thread(
                kernel.invoke_command,
                session_id,
                name,
                dict(payload.arguments),
                expected_revision=payload.expected_revision,
                client_capabilities=frozenset(payload.client_capabilities),
            )
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_confirm(self, session_id: str, payload: ConfirmRequest) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        try:
            result = await asyncio.to_thread(
                kernel.confirm_action,
                session_id,
                payload.request_id,
                payload.decision,
                expected_revision=payload.expected_revision,
                client_capabilities=frozenset(payload.client_capabilities),
            )
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_views(self, session_id: str, client_capabilities: str = Query(default="")) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        try:
            capabilities = frozenset(_capability_list_query(client_capabilities))
            snapshot = await asyncio.to_thread(kernel.plugin_views, session_id, client_capabilities=capabilities)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(snapshot.as_payload())

    async def _handle_drain(self, session_id: str, payload: DrainRequest | None = None) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        request = payload or DrainRequest()
        try:
            results = await asyncio.to_thread(kernel.drain_continuations, session_id, limit=request.limit)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok({"continuations": [item.as_payload() for item in results]})

    async def _handle_usage(
        self,
        session_id: str,
        since_sequence: int = Query(default=0, ge=0),
        until_sequence: int | None = Query(default=None, ge=0),
    ) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        try:
            report = await asyncio.to_thread(kernel.usage, session_id, since_sequence=since_sequence, until_sequence=until_sequence)
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok(report.as_payload())

    async def _handle_cancel(self, session_id: str) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        try:
            accepted = await asyncio.to_thread(kernel.cancel_session, session_id, reason=f"HTTP cancel for session {session_id}")
        except Exception as exc:
            raise _kernel_error(exc) from None
        return _ok({"cancellation_requested": bool(accepted), "session_id": session_id})

    async def _handle_events(
        self,
        session_id: str,
        after_sequence: int = Query(default=0, ge=0),
        follow: bool = Query(default=False),
    ) -> Response:
        _validated_session_id(session_id)
        kernel = self._kernel_or_503()
        try:
            record = await asyncio.to_thread(kernel.session, session_id)
            initial: list[StoredEvent] = []
            cursor = after_sequence
            while True:
                chunk = await asyncio.to_thread(kernel.events_after, session_id, after_sequence=cursor, limit=_FOLLOW_TAIL_LIMIT)
                if not chunk:
                    break
                initial.extend(chunk)
                cursor = chunk[-1].sequence
                if len(chunk) < _FOLLOW_TAIL_LIMIT:
                    break
        except Exception as exc:
            raise _kernel_error(exc) from None
        head = {
            "session": _session_payload(record),
            "events": [_event_payload(session_id, event) for event in initial],
        }

        async def frames() -> AsyncGenerator[bytes]:
            cursor = after_sequence
            if initial:
                cursor = max(event.sequence for event in initial)
            yield sse_frame(head)
            if not follow:
                return
            # Follow crosses run boundaries: completed/failed/interrupted end a
            # run, never the session. The stream ends only when the client
            # disconnects or a backend failure yields a terminal error frame.
            while True:
                await asyncio.sleep(_FOLLOW_POLL_SECONDS)
                while True:
                    try:
                        pending = await asyncio.to_thread(kernel.events_after, session_id, after_sequence=cursor, limit=_FOLLOW_TAIL_LIMIT)
                    except Exception as exc:
                        yield sse_frame({"kind": "error", "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"})
                        return
                    if not pending:
                        break
                    for event in pending:
                        cursor = event.sequence
                        yield sse_frame({"kind": "event", "session_id": session_id, "event": _event_payload(session_id, event)})
                    if len(pending) < _FOLLOW_TAIL_LIMIT:
                        break

        return EventStreamResponse(frames())


def _capability_list_query(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    for item in items:
        if "\0" in item or len(item) > 128:
            raise HttpError(400, "client_capabilities must be non-empty strings within 128 characters")
    return items


def _checkpoint_view(checkpoint: Any) -> dict[str, object] | None:
    if checkpoint is None:
        return None
    return CheckpointBody.model_validate({"kind": checkpoint.kind, "payload": json_wire_object(checkpoint.payload)}).model_dump()  # type: ignore[return-value]


def _stored_event_view(event: Any) -> dict[str, object]:
    body = StoredEventBody(
        sequence=event.sequence,
        type=event.event_type,
        run_id=event.run_id,
        revision_id=event.revision_id,
        source=event.source,
        actor=event.actor,
        payload=json_wire_object(event.payload),
    )
    return body.model_dump()  # type: ignore[return-value]


def create_kernel_app(
    *,
    workspace: Path,
    state_dir: Path | None = None,
    trusted_plugins: frozenset[str] = frozenset(),
    bearer_token: str | None = None,
    allow_origin: str | None = None,
) -> KernelTransportApp:
    resolved_workspace = workspace.resolve(strict=True)

    def factory() -> Kernel:
        return open_kernel(resolved_workspace, state_dir=state_dir, trusted_plugins=trusted_plugins)

    return KernelTransportApp(kernel_factory=factory, bearer_token=bearer_token, allow_origin=allow_origin)


def create_kernel_app_with_factory(
    *,
    kernel_factory: Callable[[], Kernel],
    bearer_token: str | None = None,
    allow_origin: str | None = None,
) -> KernelTransportApp:
    """Build the app against an explicit factory (tests and OpenAPI generation)."""
    return KernelTransportApp(kernel_factory=kernel_factory, bearer_token=bearer_token, allow_origin=allow_origin)
