"""Wire contract for the runtime HTTP transport.

The transport's request and response shapes are a client contract, so they live
in one place: the JSON envelope (byte-stable rendering plus the ``{"error",
"code"}`` error body), the pydantic boundary models for request bodies, the
mapping from pydantic validation failures onto the transport's user-facing
strings, and the hand-framed server-sent-events response.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import suppress
from http import HTTPStatus
from typing import Annotated, cast, final

from fastapi import Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ...provider.errors import validation_reason_from_error
from ..permission import PermissionResolution

logger = logging.getLogger(__name__)


def error_payload(message: str, *, code: str | None = None) -> dict[str, object]:
    """The transport's error envelope.

    ``code`` carries the runtime's machine-readable reason when the failing
    operation has one (for example ``workspace_busy``) and is ``null`` for
    errors that only know a message.
    """
    return {"error": message, "code": code}


@final
class JsonResponse(JSONResponse):
    """JSON response with the transport's byte-stable rendering.

    Keys stay sorted and non-ASCII characters escaped, and the media type keeps
    its explicit charset: both are part of the client contract.
    """

    media_type = "application/json; charset=utf-8"

    def render(self, content: object) -> bytes:
        return json.dumps(content, sort_keys=True).encode("utf-8")


def json_response(payload: object, *, status: int = 200) -> JsonResponse:
    return JsonResponse(payload, status_code=status)


def error_response(status: int, message: str, *, code: str | None = None) -> JsonResponse:
    return json_response(error_payload(message, code=code), status=status)


@final
class HttpError(HTTPException):
    """Transport error that may carry the runtime's error code."""

    def __init__(self, status_code: int, message: str, *, code: str | None = None) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code


# Starlette answers unmatched routes and wrong methods with these stock details;
# the transport has always rendered them as its own lower-case sentences.
_STOCK_DETAIL_MESSAGES = {
    HTTPStatus.NOT_FOUND.phrase: "not found",
    HTTPStatus.METHOD_NOT_ALLOWED.phrase: "method not allowed",
}

# FastAPI reports an undecodable body through this HTTPException detail instead
# of a validation error; the transport has always called that a JSON body
# failure.
_BODY_DECODE_FAILURE_DETAIL = "There was an error parsing the body"
_JSON_BODY_FAILURE_MESSAGE = "request body must be valid JSON"


def error_code(exc: BaseException) -> str | None:
    """The stable machine-readable reason a runtime error carries, if any.

    Error semantics belong to the runtime (``WorkspaceOpenError.code``,
    ``SessionSealedError.code``, ``NoPendingApprovalError.code``, ...); the
    transport only carries the value onto the wire.
    """
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else None


async def http_exception_response(_request: Request, exc: HTTPException) -> JsonResponse:
    """Render every ``HTTPException`` as the transport's error envelope."""
    detail = exc.detail
    if detail == _BODY_DECODE_FAILURE_DETAIL:
        detail = _JSON_BODY_FAILURE_MESSAGE
    else:
        detail = _STOCK_DETAIL_MESSAGES.get(detail, detail)
    return error_response(exc.status_code, detail, code=error_code(exc))


async def unhandled_exception_response(request: Request, exc: Exception) -> Response:
    """Answer an unhandled failure without leaving the transport's contract.

    Starlette re-raises after this handler runs, so the server still logs the
    traceback; the point here is that an API client never receives a body the
    transport's error envelope does not describe. Static/SPA paths keep
    Starlette's own plain-text 500 (the response the transport has always
    produced for them).
    """
    path = cast(str, request.scope.get("path", ""))
    if is_api_path(path):
        logger.error("unhandled transport failure on %s", path, exc_info=exc)
        return error_response(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")
    return PlainTextResponse("Internal Server Error", status_code=HTTPStatus.INTERNAL_SERVER_ERROR)


async def request_validation_error_response(_request: Request, exc: RequestValidationError) -> JsonResponse:
    """Render pydantic validation failures as 400 responses.

    The transport answers validation failures with 400 and one field-level
    sentence; FastAPI's default is a 422 with a list of error dictionaries.
    """
    errors = exc.errors()
    message = format_validation_error(errors[0]) if errors else _JSON_BODY_FAILURE_MESSAGE
    return error_response(HTTPStatus.BAD_REQUEST, message)


# Where FastAPI marks the location of a validated parameter. The transport has
# always named the parameter or field directly ("prompt must be ...",
# "after_sequence must be an integer"), so the marker is dropped before the
# sentence is built.
_PARAM_LOCATION_MARKERS = frozenset({"body", "query", "path", "header", "cookie"})


def _validation_loc(error: Mapping[str, object]) -> tuple[object, ...]:
    """Error location without FastAPI's leading parameter-location marker."""
    loc = tuple(cast(tuple[object, ...], error.get("loc", ())))
    return loc[1:] if loc and loc[0] in _PARAM_LOCATION_MARKERS else loc


def _http_path_from_loc(loc: tuple[object, ...]) -> str:
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            if not parts:
                parts.append(f"[{item}]")
                continue
            parts[-1] = f"{parts[-1]}[{item}]"
            continue
        parts.append(str(item))
    return ".".join(parts)


# Bodies that are not JSON objects, whatever pydantic's schema called them.
_NON_OBJECT_BODY_ERROR_TYPES = frozenset({"model_type", "model_attributes_type", "dict_type"})


def format_validation_error(error: Mapping[str, object]) -> str:
    """Turn one pydantic error into the transport's user-facing sentence."""
    loc = _validation_loc(error)
    error_type = cast(str, error.get("type", ""))
    path = _http_path_from_loc(loc)
    if error_type == "json_invalid":
        return _JSON_BODY_FAILURE_MESSAGE
    if error_type == "missing" and not path:
        # An empty request body: FastAPI reports the whole body as missing.
        return _JSON_BODY_FAILURE_MESSAGE
    if error_type == "extra_forbidden":
        unknown_keys = ", ".join(str(item) for item in loc if isinstance(item, str))
        return f"unsupported settings field(s): {unknown_keys}"
    if error_type in _NON_OBJECT_BODY_ERROR_TYPES:
        if not path:
            return "request body must be a JSON object"
        return f"{path} must be an object"
    reason = validation_reason_from_error(error)
    if not path:
        return reason
    if reason.startswith("[") or reason.startswith("."):
        return f"{path}{reason}"
    return f"{path} {reason}"


def sse_frame(payload: object) -> bytes:
    """One server-sent-events frame: ``data: <json>\\n\\n``.

    The client parsers pin this exact shape: LF separators, a literal ``data: ``
    prefix as the only frame content, and no ``id:``/``event:``/``retry:``
    fields or comment frames. The transport therefore frames its own bytes
    rather than deferring to an event-source library.
    """
    return b"data: " + json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n\n"


@final
class StreamCompletion:
    """Whether a streamed response's frame generator ran to its end.

    Starlette reports a dropped connection either as cancellation inside the
    frame generator or as a swallowed cancellation of the response task, and a
    response cancelled before its body started reports nothing at all. The
    response therefore only needs this one fact to know whether the client
    stayed for the whole stream.
    """

    __slots__ = ("finished",)

    def __init__(self) -> None:
        self.finished = False


@final
class EventStreamResponse(StreamingResponse):
    """Server-sent-events response owning the transport's disconnect story.

    Starlette consumes ``receive()`` itself and cancels the streaming task when
    the socket drops, so the transport must not compete for messages. What it
    does need is the side effect: a dropped run stream cancels the run.
    """

    media_type = "text/event-stream"

    def __init__(
        self,
        frames: AsyncGenerator[bytes],
        *,
        completion: StreamCompletion | None = None,
        on_client_disconnect: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(frames, media_type="text/event-stream", headers={"cache-control": "no-cache"})
        self._frames = frames
        self._completion = completion
        self._on_client_disconnect = on_client_disconnect
        self._on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except OSError, ClientDisconnect:
            # A failing ``send`` means the socket is gone. The transport has
            # always swallowed those instead of failing the request.
            logger.debug("client disconnected while streaming a transport response")
        finally:
            # The disconnect side effect is decided and fired before any
            # teardown runs: closing the frame generator releases the runtime
            # (``on_close``), and a failing runtime teardown must not be able to
            # swallow the cancel. The decision is made once, from the completion
            # flag, so a stream that ran to its last frame never cancels and a
            # dropped one always cancels exactly once.
            self._fire_client_disconnect()
            await self._close_frames()

    def _fire_client_disconnect(self) -> None:
        if self._on_client_disconnect is None or self._body_finished():
            return
        try:
            self._on_client_disconnect()
        except BaseException:
            # A failing cancel is still a teardown failure: report it, keep the
            # response's own outcome, and never let it pre-empt the close.
            logger.exception("failed to run the transport's client-disconnect side effect")

    def _body_finished(self) -> bool:
        return self._completion is None or self._completion.finished

    async def _close_frames(self) -> None:
        # Closing the frame generator releases the request-scoped runtime even
        # when the response never ran to completion. Teardown failures must not
        # replace the response's own outcome.
        with suppress(BaseException):
            await self._frames.aclose()
        if self._on_close is not None:
            try:
                self._on_close()
            except BaseException:
                logger.exception("failed to release the streamed response's runtime")


_API_PREFIX = "/api"


def is_api_path(path: str) -> bool:
    return path == _API_PREFIX or path.startswith(f"{_API_PREFIX}/")


_SHOW_THINKING_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _coerce_show_thinking(value: object) -> object:
    """Loose boolean read of ``show_thinking``.

    Anything that is not one of the accepting spellings is false rather than a
    validation failure: clients have always been able to send this flag
    alongside other query parameters without turning a display hint into a 400.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _SHOW_THINKING_TRUE_VALUES
    return False


def _coerce_after_sequence(value: object) -> object:
    """``after_sequence`` exactly as the transport has always parsed it.

    A blank value means the default cursor (0), the digits go through ``int``
    unchanged, and the two failure sentences stay the transport's own.
    """
    if isinstance(value, str) and not value.strip():
        return 0
    try:
        sequence = int(cast("str | int", value))
    except TypeError, ValueError:
        raise ValueError("must be an integer") from None
    if sequence < 0:
        raise ValueError("must be non-negative")
    return sequence


def _coerce_follow(value: object) -> object:
    """``follow`` exactly as the transport has always parsed it (only ``true``)."""
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() == "true"


# Query parameters of the session-event stream and the reasoning display flag.
# They are declared as typed ``Query`` parameters so the route contract shows up
# in the OpenAPI document instead of hiding in a raw-query dependency.
AfterSequenceQuery = Annotated[
    int,
    Query(
        summary="Deliver only events after this sequence",
        description=(
            "Cursor over the session's ordered events: the stream opens with every event whose "
            "`sequence` is greater than this value. Blank means 0; negative values are rejected."
        ),
    ),
    BeforeValidator(_coerce_after_sequence),
]
FollowQuery = Annotated[
    bool,
    Query(
        summary="Keep streaming events after the initial replay",
        description=(
            "When true the stream stays open and delivers new events as they are appended, closing once the session reaches a terminal status."
        ),
    ),
    BeforeValidator(_coerce_follow),
]
ShowThinkingQuery = Annotated[
    bool,
    Query(
        summary="Include reasoning content in the response",
        description=(
            "Reasoning/thinking payloads are redacted by default (`show_thinking=false`); "
            "true returns them unredacted in the events and transcripts this route returns. "
            "Accepted spellings are `true`/`1`/`yes`/`on` (case-insensitive); anything else means false."
        ),
    ),
    BeforeValidator(_coerce_show_thinking),
]


@final
class JsonBodyContentTypeMiddleware:
    """Parse every API request body as JSON, whatever the header claims.

    The transport has always decoded request bodies as JSON without consulting
    ``content-type``. FastAPI keeps the raw bytes instead when the header is
    absent or declares a non-JSON media type, so normalize it on the API paths
    that own a body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_api_path(cast(str, scope.get("path", ""))):
            headers = [(name, value) for name, value in scope.get("headers", []) if name.lower() != b"content-type"]
            scope["headers"] = [*headers, (b"content-type", b"application/json")]
        await self._app(scope, receive, send)


class _HttpBoundaryModel(BaseModel):
    """Base for a request body whose fields the route requires.

    A field declares the type it has *after* validation with
    ``Field(default=None, validate_default=True)``: the validator then runs on the
    default of a missing field, so an absent field fails with that field's own
    sentence ("prompt must be a non-empty string") instead of pydantic's
    "Field required", and the attribute reads as its real type at the route
    instead of a ``cast``. ``validate_default`` is spelled on each such field
    because that is the overload pydantic types as "the default need not match
    the field's type".
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)


class _RunStreamRequestPayload(_HttpBoundaryModel):
    prompt: str = Field(default=None, validate_default=True)
    session_id: str | None = None
    parent_session_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("prompt", mode="before")
    @classmethod
    def _validate_prompt(cls, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("session_id", "parent_session_id", mode="before")
    @classmethod
    def _validate_optional_string(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string when provided")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def _validate_metadata(cls, value: object) -> dict[str, object]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("must be an object when provided")
        return value


class _ApprovalResolutionRequestPayload(_HttpBoundaryModel):
    request_id: str = Field(default=None, validate_default=True)
    decision: PermissionResolution = Field(default=None, validate_default=True)

    @field_validator("request_id", mode="before")
    @classmethod
    def _validate_request_id(cls, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("decision", mode="before")
    @classmethod
    def _validate_decision(cls, value: object) -> PermissionResolution:
        if value == "allow":
            return "allow"
        if value == "deny":
            return "deny"
        raise ValueError("must be 'allow' or 'deny'")


class _SessionCancelRequestPayload(_HttpBoundaryModel):
    run_id: str | None = None
    reason: str | None = None

    @field_validator("run_id", "reason", mode="before")
    @classmethod
    def _validate_optional_string(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string when provided")
        stripped = value.strip()
        return stripped or None


class _SettingsRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    provider_api_key: str | None = None
    model: str | None = None

    @field_validator("provider", "provider_api_key", "model", mode="before")
    @classmethod
    def _validate_optional_string(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string when provided")
        stripped = value.strip()
        return stripped or None


class _WorkspaceOpenRequestPayload(BaseModel):
    """``POST /api/workspaces/open`` body.

    Unknown keys stay tolerated (the transport ignored them) and the blank-path
    sentence is the route's own.
    """

    model_config = ConfigDict(validate_default=True)

    path: str = Field(default=None, validate_default=True)

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class _TaskSteerRequestPayload(BaseModel):
    """``POST /api/tasks/{id}/steer`` body.

    The route has always reported its own sentences for a missing prompt and for
    a body that is not an object, so both live in the model instead of the
    generic field-level wording: one ``before`` validator answers both, which
    keeps the sentences path-free (a field validator would prefix them with the
    field name) and leaves ``prompt`` typed as the string the route reads.
    """

    prompt: str = Field(default=None, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def _validate_body(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object with a 'prompt' field")
        prompt = value.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("request body 'prompt' must be a non-empty string")
        return value


class _QuestionResponsePayload(_HttpBoundaryModel):
    header: str = Field(default=None, validate_default=True)
    answers: tuple[str, ...] = Field(default=None, validate_default=True)

    @field_validator("header", mode="before")
    @classmethod
    def _validate_header(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("answers", mode="before")
    @classmethod
    def _validate_answers(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not value:
            raise ValueError("must be a non-empty array")
        answers: list[str] = []
        for index, raw_answer in enumerate(value):
            if not isinstance(raw_answer, str) or not raw_answer.strip():
                raise ValueError(f"[{index}] must be a non-empty string")
            answers.append(raw_answer)
        return tuple(answers)


class _QuestionAnswerRequestPayload(_HttpBoundaryModel):
    request_id: str = Field(default=None, validate_default=True)
    responses: tuple[_QuestionResponsePayload, ...] | None = None

    @field_validator("request_id", mode="before")
    @classmethod
    def _validate_request_id(cls, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("responses", mode="before")
    @classmethod
    def _validate_responses(cls, value: object) -> list[object]:
        if not isinstance(value, list) or not value:
            raise ValueError("must be a non-empty array")
        return value


class _SessionRevertRequestPayload(_HttpBoundaryModel):
    sequence: int = Field(default=None, validate_default=True)

    @field_validator("sequence", mode="before")
    @classmethod
    def _validate_sequence(cls, value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError("must be a positive integer")
        return value


class _SteerSessionRequestPayload(_HttpBoundaryModel):
    content: str = Field(default=None, validate_default=True)

    @field_validator("content", mode="before")
    @classmethod
    def _validate_content(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value
