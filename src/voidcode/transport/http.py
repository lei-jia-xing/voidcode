"""Core HTTP transport: a small FastAPI app over one Core authority.

Routes are thin projections of ``voidcode.core.engine.Core`` methods.
Request bodies validate once at ingress (see ``models.py``); responses render
byte-stable JSON or hand-framed SSE. The app never invents commands, views,
or provider calls — errors from the core surface verbatim.
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
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import Receive, Scope, Send

from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import SessionRecord, StoredEvent, inspect_existing_workspace
from voidcode.sdk.client import HealthStatus, RunResult, SessionRow, SseFrameWire
from voidcode.sdk.commands import CommandCatalog, CommandResult
from voidcode.sdk.services import SessionUsageReport
from voidcode.sdk.transcript import ContextHistoryWire, TranscriptPageWire, encode_context_history, encode_transcript_page
from voidcode.sdk.views import ViewsSnapshot

from .models import (
    ConfirmRequest,
    ContinuationsBody,
    CreateSessionRequest,
    DrainRequest,
    InvokeRequest,
    ResumeRequest,
    RunRequest,
    SessionsBody,
    ShowBody,
)
from .projection import (
    FOLLOW_POLL_SECONDS,
    FOLLOW_TAIL_LIMIT,
    core_error_status,
    event_payload,
    health_payload,
    safe_projection_error,
    session_payload,
    validated_session_id,
)

logger = logging.getLogger(__name__)
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
    logger.error("unhandled transport failure on %s", request.scope.get("path", ""), exc_info=exc)
    return _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")


def _core_error(error: Exception) -> HttpError:
    status, code = core_error_status(error)
    if status == 500:
        return HttpError(500, "internal server error")
    return HttpError(status, str(error), code=code)


def _validated_session_id(value: str) -> str:
    try:
        return validated_session_id(value)
    except ValueError as exc:
        raise HttpError(400, str(exc)) from None


def _error_event(session_id: str, error: str) -> ServerSentEvent:
    return ServerSentEvent(data=json.dumps({"kind": "error", "session_id": session_id, "error": error}, sort_keys=True, allow_nan=False), sep="\n")


class CoreTransportApp(FastAPI):
    """The core's HTTP transport as a FastAPI application."""

    def __init__(
        self,
        *,
        core_factory: Callable[[], Core | None],
        bearer_token: str | None = None,
        allow_origin: str | None = None,
        frontend_dist: Path | None = None,
        fenced: bool = False,
        control_token: str | None = None,
        generation: int = 1,
        upgrade_id: str | None = None,
        workspace: Path | None = None,
        state_dir: Path | None = None,
    ) -> None:
        super().__init__(
            title="VoidCode core API",
            description='Generic core transport. Every /api error uses the {"error", "code"} envelope.',
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
        self._core_factory = core_factory
        self._core: Core | None = None
        self._bearer_token = bearer_token
        self._allow_origin = allow_origin
        self._fenced = fenced
        self._control_token = control_token
        self._generation = generation
        self._upgrade_id = upgrade_id
        self._workspace = workspace
        self._state_dir = state_dir
        # An app born unfenced is already serving; a fenced child reports its validation
        # stage until promoted.
        self._stage = "serving" if not fenced else "validating"
        # Live follow streams, for the drain to wait on: a successful drain returns only after
        # every open stream ended with its restart frame, so no SIGTERM can race frame delivery.
        self._follow_streams: set[asyncio.Event] = set()
        # Two-phase drain: business writes fence the moment a drain arrives, but the SSE
        # restart frame goes out only once a drain has actually succeeded.
        self._restart_ready = False
        #: The static web bundle this process serves at "/", when one is built. The bundle is
        #: public: only /api carries the bearer token.
        self._frontend_dist = frontend_dist.resolve() if frontend_dist is not None and frontend_dist.is_dir() else None
        self._register_routes()

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncGenerator[None]:
        core = await asyncio.to_thread(self._core_factory)
        self._core = core
        # The factory returning at all is the validation: a child whose generation cannot be
        # served raises inside the factory instead, and uvicorn exits non-zero.
        if self._fenced:
            self._stage = "validated"
        try:
            yield
        finally:
            self._core = None
            if core is not None:
                await asyncio.to_thread(core.close)

    def _core_or_503(self) -> Core:
        if self._core is None:
            raise HttpError(503, "server is not started")
        return self._core

    def _require_serving(self) -> None:
        pass

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in ("http", "lifespan"):
            raise RuntimeError(f"unsupported scope type: {scope.get('type')!r}")
        if scope.get("type") == "http":
            path = str(scope.get("path", ""))
            if path == "/api" or path.startswith("/api/"):
                challenge = self._auth_challenge(scope)
                if challenge is not None:
                    await challenge(scope, receive, send)
                    return
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

        from voidcode.sdk.client import ErrorFrameWire, EventFrameWire, HeadFrameWire, RestartFrameWire, StreamEventWire
        from voidcode.sdk.transcript import TranscriptEntryWire as EntryWire
        from voidcode.sdk.views import (
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
        wire_classes = [StreamEventWire, HeadFrameWire, EventFrameWire, ErrorFrameWire, RestartFrameWire, EntryWire]
        for cls in [*view_classes, *wire_classes]:
            s = TypeAdapter(cls).json_schema(ref_template="#/components/schemas/{model}")
            defs = s.pop("$defs", None)
            if defs:
                for def_name, def_schema in defs.items():
                    schemas[def_name] = def_schema
            schemas[cls.__name__] = s

        frame_defs = TypeAdapter(SseFrameWire).json_schema(ref_template="#/components/schemas/{model}").get("$defs", {})
        for def_name, def_schema in frame_defs.items():
            schemas.setdefault(def_name, def_schema)
        frame_refs = [{"$ref": f"#/components/schemas/{name}"} for name in ("HeadFrameWire", "EventFrameWire", "ErrorFrameWire", "RestartFrameWire")]
        schemas["SseFrameWire"] = {"oneOf": frame_refs}
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
        streaming_frame: Any = None,
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
            response_class = EventSourceResponse
        self.add_api_route(
            path,
            endpoint,
            methods=methods,
            tags=["core"],
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
        route("/api/sessions", self._handle_create_session, methods=["POST"], summary="Create a session", response_model=SessionRow, status_code=201)  # noqa: E501
        route("/api/sessions/{session_id}", self._handle_get_session, methods=["GET"], summary="Get a session", response_model=SessionRow)
        route("/api/sessions/{session_id}/show", self._handle_show_session, methods=["GET"], summary="Show session state", response_model=ShowBody)
        route("/api/sessions/{session_id}/run", self._handle_run, methods=["POST"], summary="Run an agent turn", response_model=RunResult)
        route("/api/sessions/{session_id}/resume", self._handle_resume, methods=["POST"], summary="Resume a session", response_model=RunResult)
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
        route(
            "/api/sessions/{session_id}/transcript",
            self._handle_transcript,
            methods=["GET"],
            summary="Read a bounded transcript page",
            response_model=TranscriptPageWire,
        )
        route(
            "/api/sessions/{session_id}/context",
            self._handle_context,
            methods=["GET"],
            summary="Read a bounded context prefix",
            response_model=ContextHistoryWire,
        )
        route("/api/sessions/{session_id}/cancel", self._handle_cancel, methods=["POST"], summary="Request cancellation for one session")
        route(
            "/api/sessions/{session_id}/events", self._handle_events, methods=["GET"], summary="Replay session events", streaming_frame=SseFrameWire
        )  # noqa: E501
        route("/api/health", self._handle_health, methods=["GET"], summary="Read core health and identity", response_model=HealthStatus)
        if self._frontend_dist is not None:
            # The single-page client: an exact file when one exists, otherwise index.html, so a
            # client-side route survives a reload. Registered last so every /api route wins, and
            # never in the OpenAPI document: it is not part of the API contract.
            self.add_api_route("/{path:path}", self._handle_static, methods=["GET"], include_in_schema=False, response_class=FileResponse)

    async def _handle_static(self, path: str) -> Response:
        dist = self._frontend_dist
        if dist is None:  # pragma: no cover - the route is only registered with a bundle
            raise HttpError(404, "not found", code="not_found")
        if path == "api" or path.startswith("api/"):
            raise HttpError(404, "not found", code="not_found")
        if path:
            candidate = (dist / path).resolve()
            # A traversal or absolute path resolves outside the bundle and falls through to the
            # SPA entry point instead of reading an arbitrary file.
            if candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    async def _handle_openapi_document(self) -> Response:
        return _ok(self.openapi())

    async def _handle_command_catalog(self) -> Response:
        core = self._core_or_503()
        try:
            catalog = await asyncio.to_thread(core.command_catalog)
        except Exception as exc:
            raise _core_error(exc) from None
        # One immutable snapshot: descriptors and the revision they were captured
        # from travel together, so a client can echo it verbatim later.
        return _ok(catalog.as_payload())

    async def _handle_list_sessions(self, limit: int = Query(default=100, ge=1, le=1000)) -> Response:
        core = self._core_or_503()
        try:
            records = await asyncio.to_thread(core.sessions, limit=limit)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok({"sessions": [session_payload(record) for record in records]})

    async def _handle_create_session(self, payload: CreateSessionRequest | None = None) -> Response:
        self._require_serving()
        core = self._core_or_503()
        requested = payload.session_id if payload is not None else None
        if requested is not None:
            if not requested:
                raise HttpError(400, "session_id must be a non-empty string", code="invalid_params")
            _validated_session_id(requested)
        try:
            record = await asyncio.to_thread(core.create_session, requested)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(session_payload(record), status=201)

    async def _handle_get_session(self, session_id: str) -> Response:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            record = await asyncio.to_thread(core.session, session_id)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(session_payload(record))

    async def _handle_show_session(self, session_id: str, events: bool = Query(default=False)) -> Response:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            payload = await asyncio.to_thread(core.show, session_id, events=events)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(payload)

    async def _handle_run(self, session_id: str, payload: RunRequest) -> Response:
        _validated_session_id(session_id)
        self._require_serving()
        core = self._core_or_503()
        if payload.session_id is not None and payload.session_id != session_id:
            raise HttpError(400, "session_id in the path and body must match")
        try:
            result = await asyncio.to_thread(core.run, payload.prompt, session_id=session_id, max_steps=payload.max_steps)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_resume(self, session_id: str, payload: ResumeRequest | None = None) -> Response:
        _validated_session_id(session_id)
        self._require_serving()
        core = self._core_or_503()
        request = payload or ResumeRequest()
        try:
            result = await asyncio.to_thread(
                core.resume,
                session_id,
                approval_request_id=request.approval_request_id,
                decision=request.decision,
                max_steps=request.max_steps,
            )
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_invoke(self, session_id: str, name: str, payload: InvokeRequest) -> Response:
        _validated_session_id(session_id)
        if not name or "/" in name or "\0" in name or len(name) > 128:
            raise HttpError(400, "command name must be a non-empty string without '/' or NUL within 128 characters")
        self._require_serving()
        core = self._core_or_503()
        try:
            result = await asyncio.to_thread(
                core.invoke_command,
                session_id,
                name,
                dict(payload.arguments),
                expected_revision=payload.expected_revision,
            )
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_confirm(self, session_id: str, payload: ConfirmRequest) -> Response:
        _validated_session_id(session_id)
        self._require_serving()
        core = self._core_or_503()
        try:
            result = await asyncio.to_thread(
                core.confirm_action,
                session_id,
                payload.request_id,
                payload.decision,
                expected_revision=payload.expected_revision,
            )
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(result.as_payload())

    async def _handle_views(self, session_id: str) -> Response:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            snapshot = await asyncio.to_thread(core.plugin_views, session_id)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(snapshot.as_payload())

    async def _handle_drain(self, session_id: str, payload: DrainRequest | None = None) -> Response:
        _validated_session_id(session_id)
        self._require_serving()
        core = self._core_or_503()
        request = payload or DrainRequest()
        try:
            results = await asyncio.to_thread(core.drain_continuations, session_id, limit=request.limit)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok({"continuations": [item.as_payload() for item in results]})

    async def _handle_usage(
        self,
        session_id: str,
        since_sequence: int = Query(default=0, ge=0),
        until_sequence: int | None = Query(default=None, ge=0),
    ) -> Response:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            report = await asyncio.to_thread(core.usage, session_id, since_sequence=since_sequence, until_sequence=until_sequence)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(report.as_payload())

    async def _handle_transcript(
        self,
        session_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> Response:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            page = await asyncio.to_thread(core.transcript_page, session_id, after_sequence=after_sequence, limit=limit)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(encode_transcript_page(page))

    async def _handle_context(
        self,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> Response:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            history = await asyncio.to_thread(core.context_history, session_id, limit=limit)
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok(encode_context_history(history))

    async def _handle_health(self) -> Response:
        core = self._core
        revision_id = ""
        if core is not None:
            revision_id = core.revision_id
        elif self._workspace is not None and self._state_dir is not None:
            database = self._state_dir / "voidcode.db"
            if database.is_file():
                try:
                    found_revision, _found_payload, _ = await asyncio.to_thread(inspect_existing_workspace, database, str(self._workspace))
                except Exception:
                    found_revision = None
                if found_revision is not None:
                    revision_id = found_revision
        return _ok(
            health_payload(
                revision_id=revision_id,
                stage=self._stage,
                workspace="" if self._workspace is None else str(self._workspace),
                generation=self._generation,
                upgrade_id=self._upgrade_id,
            )
        )

    async def _handle_cancel(self, session_id: str) -> Response:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            accepted = await asyncio.to_thread(core.cancel_session, session_id, reason=f"HTTP cancel for session {session_id}")
        except Exception as exc:
            raise _core_error(exc) from None
        return _ok({"cancellation_requested": bool(accepted), "session_id": session_id})

    async def _handle_events(
        self,
        session_id: str,
        after_sequence: int = Query(default=0, ge=0),
        follow: bool = Query(default=False),
    ) -> EventSourceResponse:
        _validated_session_id(session_id)
        core = self._core_or_503()
        try:
            record = await asyncio.to_thread(core.session, session_id)
        except Exception as exc:
            raise _core_error(exc) from None
        return EventSourceResponse(
            self._follow_frames(core, session_id, after_sequence, follow=follow, record=record),
            headers={"cache-control": "no-cache"},
            sep="\n",
        )

    async def _follow_frames(
        self,
        core: Core,
        session_id: str,
        after_sequence: int,
        follow: bool = False,
        record: SessionRecord | None = None,
    ) -> AsyncGenerator[ServerSentEvent]:
        """Stream initial events then follow until disconnect, error, or the drain's restart frame."""
        if record is None:
            record = await asyncio.to_thread(core.session, session_id)
        initial: list[StoredEvent] = []
        cursor = after_sequence
        while True:
            try:
                chunk = await asyncio.to_thread(core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
            except Exception as exc:
                yield _error_event(session_id, f"event read failed: {type(exc).__name__}")
                return
            if not chunk:
                break
            initial.extend(chunk)
            cursor = chunk[-1].sequence
            if len(chunk) < FOLLOW_TAIL_LIMIT:
                break
        frames: list[dict[str, object]] = []
        for event in initial:
            try:
                frames.append(event_payload(session_id, event))
            except Exception as exc:
                logger.warning("head projection failed: %s", safe_projection_error(event, exc))
                yield _error_event(session_id, safe_projection_error(event, exc))
                return
        head = {"session": session_payload(record), "events": frames}
        yield ServerSentEvent(data=json.dumps(head, sort_keys=True, allow_nan=False), sep="\n")
        if not follow:
            return
        done = asyncio.Event()
        self._follow_streams.add(done)
        try:
            while True:
                await asyncio.sleep(FOLLOW_POLL_SECONDS)
                while True:
                    try:
                        pending = await asyncio.to_thread(core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
                    except Exception as exc:
                        if self._restart_ready:
                            break
                        yield _error_event(session_id, f"event read failed: {type(exc).__name__}")
                        return
                    if not pending:
                        break
                    for event in pending:
                        try:
                            frame = {"kind": "event", "session_id": session_id, "event": event_payload(session_id, event)}
                        except Exception as exc:
                            logger.warning("live projection failed: %s", safe_projection_error(event, exc))
                            yield _error_event(session_id, safe_projection_error(event, exc))
                            return
                        cursor = event.sequence
                        yield ServerSentEvent(data=json.dumps(frame, sort_keys=True, allow_nan=False), sep="\n")
                    if len(pending) < FOLLOW_TAIL_LIMIT:
                        break
                if self._restart_ready:
                    while True:
                        try:
                            tail = await asyncio.to_thread(core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
                        except Exception:
                            break
                        if not tail:
                            break
                        for event in tail:
                            try:
                                frame = {"kind": "event", "session_id": session_id, "event": event_payload(session_id, event)}
                            except Exception as exc:
                                logger.warning("drain projection failed: %s", safe_projection_error(event, exc))
                                yield _error_event(session_id, safe_projection_error(event, exc))
                                return
                            cursor = event.sequence
                            yield ServerSentEvent(data=json.dumps(frame, sort_keys=True, allow_nan=False), sep="\n")
                    yield ServerSentEvent(
                        data=json.dumps(
                            {"kind": "server_restart", "session_id": session_id, "cursor": cursor},
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        sep="\n",
                    )
                    return
        finally:
            self._follow_streams.discard(done)
            done.set()


def create_core_app(
    *,
    workspace: Path,
    state_dir: Path | None = None,
    bearer_token: str | None = None,
    allow_origin: str | None = None,
    frontend_dist: Path | None = None,
    fenced: bool = False,
    control_token: str | None = None,
    generation: int = 1,
    upgrade_id: str | None = None,
) -> CoreTransportApp:
    resolved_workspace = workspace.resolve(strict=True)
    resolved_state = None if state_dir is None else Path(state_dir)

    def factory() -> Core | None:
        return open_core(resolved_workspace, state_dir=resolved_state)

    return CoreTransportApp(
        core_factory=factory,
        bearer_token=bearer_token,
        allow_origin=allow_origin,
        frontend_dist=frontend_dist,
        fenced=fenced,
        control_token=control_token,
        generation=generation,
        upgrade_id=upgrade_id,
        workspace=resolved_workspace,
        state_dir=resolved_state,
    )


def create_core_app_with_factory(
    *,
    core_factory: Callable[[], Core | None],
    bearer_token: str | None = None,
    allow_origin: str | None = None,
    frontend_dist: Path | None = None,
    fenced: bool = False,
    control_token: str | None = None,
    generation: int = 1,
    upgrade_id: str | None = None,
    workspace: Path | None = None,
    state_dir: Path | None = None,
) -> CoreTransportApp:
    """Build the app against an explicit factory (tests and OpenAPI generation)."""
    return CoreTransportApp(
        core_factory=core_factory,
        bearer_token=bearer_token,
        allow_origin=allow_origin,
        frontend_dist=frontend_dist,
        fenced=fenced,
        control_token=control_token,
        generation=generation,
        upgrade_id=upgrade_id,
        workspace=workspace,
        state_dir=state_dir,
    )
