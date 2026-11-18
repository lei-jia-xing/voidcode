"""WebSocket RPC application, with HTTP only for health, schema, and static assets."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import DATABASE_FILENAME, inspect_existing_workspace
from voidcode.sdk.client import HealthStatus, SessionFrameWire
from voidcode.sdk.rpc import RPC_MAX_MESSAGE_BYTES, RPC_SUBPROTOCOL, RpcFailure, RpcNotification, RpcRequest, RpcSuccess
from voidcode.sdk.views import (
    ChoicesViewData,
    ConfirmationViewData,
    DiffViewData,
    FieldsViewData,
    FormViewData,
    JsonViewData,
    MarkdownViewData,
    MediaViewData,
    NoticeViewData,
    StatusViewData,
    TableViewData,
)

from .models import AuthenticateParams, SessionFrameParams, SubscriptionClosedParams
from .projection import health_payload, model_payload
from .rpc import CONNECTION_METHODS, METHODS, RpcConnection, failure, parse_json


class JsonResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"

    def render(self, content: object) -> bytes:
        return json.dumps(content, sort_keys=True, allow_nan=False).encode("utf-8")


def _error(status: int, message: str, code: str | None = None) -> JsonResponse:
    return JsonResponse({"error": message, "code": code}, status_code=status)


async def _http_exception_handler(_request: Request, exc: HTTPException) -> Response:
    message = "not found" if exc.status_code == 404 else str(exc.detail)
    return _error(exc.status_code, message)


class CoreTransportApp(FastAPI):
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
            title="VoidCode RPC contract",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
            redirect_slashes=False,
            lifespan=self._lifespan,
            exception_handlers={HTTPException: _http_exception_handler},
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
        self._stage = "serving" if not fenced else "validating"
        self._frontend_dist = frontend_dist.resolve() if frontend_dist is not None and frontend_dist.is_dir() else None
        self._rpc_calls: set[asyncio.Task[None]] = set()
        self.add_api_route("/api/health", self._handle_health, response_model=HealthStatus)
        self.add_api_route("/api/openapi.json", self._handle_schema, include_in_schema=False)
        self.add_api_websocket_route("/api/rpc", self._handle_rpc)
        if self._frontend_dist is not None:
            self.add_api_route("/{path:path}", self._handle_static, include_in_schema=False)

    @asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncGenerator[None]:
        core = await asyncio.to_thread(self._core_factory)
        self._core = core
        if self._fenced:
            self._stage = "validated"
        try:
            yield
        finally:
            if core is not None:
                await asyncio.to_thread(core.close)
            if self._rpc_calls:
                await asyncio.gather(*self._rpc_calls, return_exceptions=True)
            self._core = None

    def _origin_allowed(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin")
        return self._allow_origin is None or origin is None or origin.decode("latin-1") == self._allow_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (scope["path"] == "/api" or scope["path"].startswith("/api/")):
            headers = dict(scope.get("headers", []))
            presented = headers.get(b"authorization", b"").decode("latin-1")
            scheme, _, token = presented.partition(" ")
            if self._bearer_token is not None and (
                scheme.lower() != "bearer" or not secrets.compare_digest(token.encode("utf-8"), self._bearer_token.encode("utf-8"))
            ):
                await _error(401, "missing or invalid bearer token", "unauthorized")(scope, receive, send)
                return
            if not self._origin_allowed(scope):
                await _error(403, "origin is not allowed", "forbidden_origin")(scope, receive, send)
                return
            origin = headers.get(b"origin")
            if scope.get("method") == "OPTIONS":
                await Response(
                    status_code=204,
                    headers={
                        "access-control-allow-origin": origin.decode("latin-1") if origin else "*",
                        "access-control-allow-methods": "GET, OPTIONS",
                        "access-control-allow-headers": "Authorization, Content-Type",
                    },
                )(scope, receive, send)
                return
            if origin:

                async def cors_send(message: Any) -> None:
                    if message.get("type") == "http.response.start":
                        message["headers"] = [*message.get("headers", []), (b"access-control-allow-origin", origin)]
                    await send(message)

                await super().__call__(scope, receive, cors_send)
                return
        await super().__call__(scope, receive, send)

    async def _handle_rpc(self, websocket: WebSocket) -> None:
        if not self._origin_allowed(websocket.scope) or RPC_SUBPROTOCOL not in websocket.scope.get("subprotocols", []):
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=RPC_SUBPROTOCOL)
        try:
            text = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            if len(text.encode("utf-8")) > RPC_MAX_MESSAGE_BYTES:
                await websocket.close(code=1009)
                return
            request = RpcRequest.model_validate(parse_json(text))
            params = AuthenticateParams.model_validate(request.params)
            valid = request.method == "connection.authenticate" and "id" in request.model_fields_set
            valid = valid and (
                self._bearer_token is None
                or (params.token is not None and secrets.compare_digest(params.token.encode("utf-8"), self._bearer_token.encode("utf-8")))
            )
            if not valid:
                await websocket.send_json(failure(request.id, -32001, "Authentication required", data={"code": "unauthorized", "status": 401}))
                await websocket.close(code=1008)
                return
            core = self._core
            if core is None:
                await websocket.send_json(failure(request.id, -32000, "server is not started", data={"status": 503}))
                await websocket.close(code=1013)
                return
            await websocket.send_json({"jsonrpc": "2.0", "id": request.id, "result": {"protocol_version": 1}})
            await RpcConnection(websocket, core, self._rpc_calls).serve()
        except TimeoutError, ValueError, ValidationError:
            await websocket.close(code=1008, reason="invalid authentication")
        except WebSocketDisconnect, RuntimeError:
            return

    async def _handle_schema(self) -> Response:
        return JsonResponse(self.openapi())

    def openapi(self) -> dict[str, Any]:  # type: ignore[override]
        doc = super().openapi()
        schemas = doc.setdefault("components", {}).setdefault("schemas", {})
        view_classes = [
            NoticeViewData,
            MarkdownViewData,
            FieldsViewData,
            ChoicesViewData,
            ConfirmationViewData,
            FormViewData,
            DiffViewData,
            StatusViewData,
            TableViewData,
            JsonViewData,
            MediaViewData,
        ]
        declarations: list[tuple[str, Any]] = [(cls.__name__, cls) for cls in [*view_classes, RpcRequest, RpcSuccess, RpcFailure, RpcNotification]]
        methods = {name: (method.params, method.result) for name, method in METHODS.items()} | CONNECTION_METHODS
        for params, result in methods.values():
            declarations.extend([(params.__name__, params), (result.__name__, result)])
        declarations.append(("SessionFrameWire", SessionFrameWire))
        notifications = {"session.frame": SessionFrameParams, "subscription.closed": SubscriptionClosedParams}
        declarations.extend((cls.__name__, cls) for cls in notifications.values())
        for name, cls in declarations:
            schema = TypeAdapter(cls).json_schema(ref_template="#/components/schemas/{model}")
            schemas.update(schema.pop("$defs", {}))
            schemas[name] = schema
        schemas["PluginViewData"] = {"oneOf": [{"$ref": f"#/components/schemas/{cls.__name__}"} for cls in view_classes]}
        schemas["PluginViewDescriptor"]["properties"]["data"] = {"$ref": "#/components/schemas/PluginViewData"}
        schemas["CommandPresentationData"] = {
            "oneOf": [{"$ref": f"#/components/schemas/{cls.__name__}"} for cls in view_classes if cls is not ConfirmationViewData]
        }
        schemas["CommandPresentation"]["properties"]["data"] = {"$ref": "#/components/schemas/CommandPresentationData"}
        doc["x-rpc-methods"] = {
            name: {
                "params": {"$ref": f"#/components/schemas/{params.__name__}"},
                "result": {"$ref": f"#/components/schemas/{result.__name__}"},
            }
            for name, (params, result) in methods.items()
        }
        doc["x-rpc-notifications"] = {name: {"$ref": f"#/components/schemas/{cls.__name__}"} for name, cls in notifications.items()}
        doc["x-rpc-transport"] = {"path": "/api/rpc", "subprotocol": RPC_SUBPROTOCOL}
        return doc

    async def _handle_health(self) -> Response:
        revision_id = self._core.revision_id if self._core is not None else ""
        if not revision_id and self._workspace is not None and self._state_dir is not None:
            database = self._state_dir / DATABASE_FILENAME
            if database.is_file():
                try:
                    found, _payload, _ = await asyncio.to_thread(inspect_existing_workspace, database, str(self._workspace))
                    revision_id = found or ""
                except Exception:
                    pass
        model = None
        if self._core is not None:
            try:
                config = await asyncio.to_thread(lambda: self._core.config if self._core is not None else None)
            except Exception:
                config = None
            if config is not None:
                model = model_payload(raw=config.model, configured_effort=config.reasoning_effort)
        return JsonResponse(
            health_payload(
                revision_id=revision_id,
                stage=self._stage,
                workspace=str(self._workspace) if self._workspace else "",
                generation=self._generation,
                upgrade_id=self._upgrade_id,
                model=model,
            )
        )

    async def _handle_static(self, path: str) -> Response:
        dist = self._frontend_dist
        if dist is None or path == "api" or path.startswith("api/"):
            return _error(404, "not found", "not_found")
        candidate = (dist / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")


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
