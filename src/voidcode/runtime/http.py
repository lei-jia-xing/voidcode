from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Protocol, cast, final

from .config import RuntimeConfig
from .contracts import (
    NoPendingQuestionError,
    RuntimeNotification,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeResponse,
    RuntimeSessionResult,
    RuntimeStreamChunk,
    validate_runtime_request_metadata,
    validate_session_id,
    validate_session_reference_id,
)
from .events import EventEnvelope
from .permission import PermissionResolution
from .question import QuestionResponse
from .service import VoidCodeRuntime
from .session import SessionRef, SessionState, StoredSessionSummary

logger = logging.getLogger(__name__)


class RuntimeTransport(Protocol):
    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]: ...

    def web_settings(self) -> dict[str, object]: ...

    def update_web_settings(
        self,
        *,
        provider: str | None = None,
        provider_api_key: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]: ...

    def session_result(self, *, session_id: str) -> RuntimeSessionResult: ...

    def list_notifications(self) -> tuple[RuntimeNotification, ...]: ...

    def acknowledge_notification(self, *, notification_id: str) -> RuntimeNotification: ...

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


class Receive(Protocol):
    async def __call__(self) -> dict[str, object]: ...


class Send(Protocol):
    async def __call__(self, message: dict[str, object]) -> None: ...


@final
class RuntimeTransportApp:
    _runtime_factory: Callable[[], RuntimeTransport]

    def __init__(self, *, runtime_factory: Callable[[], RuntimeTransport]) -> None:
        self._runtime_factory = runtime_factory

    @staticmethod
    def _close_runtime(runtime: RuntimeTransport) -> None:
        exit_method = getattr(runtime, "__exit__", None)
        if callable(exit_method):
            exit_method(None, None, None)

    async def __call__(
        self,
        scope: dict[str, object],
        receive: Receive,
        send: Send,
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope_type != "http":
            raise RuntimeError(f"unsupported scope type: {scope_type!r}")

        method = cast(str, scope.get("method", "GET"))
        path = cast(str, scope.get("path", "/"))

        if path == "/api/runtime/run/stream":
            if method != "POST":
                await self._json_response(
                    send,
                    status=405,
                    payload={"error": "method not allowed"},
                )
                return
            await self._handle_run_stream(receive, send)
            return

        if path == "/api/sessions":
            if method != "GET":
                await self._json_response(
                    send,
                    status=405,
                    payload={"error": "method not allowed"},
                )
                return
            await self._handle_list_sessions(send)
            return

        if path == "/api/notifications":
            if method != "GET":
                await self._json_response(
                    send,
                    status=405,
                    payload={"error": "method not allowed"},
                )
                return
            await self._handle_list_notifications(send)
            return

        if path == "/api/settings":
            if method == "GET":
                await self._handle_get_settings(send)
                return
            if method == "POST":
                await self._handle_update_settings(receive, send)
                return
            await self._json_response(
                send,
                status=405,
                payload={"error": "method not allowed"},
            )
            return

        notification_prefix = "/api/notifications/"
        if path.startswith(notification_prefix):
            notification_path = path.removeprefix(notification_prefix)
            if not notification_path.endswith("/ack"):
                await self._json_response(send, status=404, payload={"error": "not found"})
                return
            notification_id = notification_path.removesuffix("/ack")
            if not notification_id:
                await self._json_response(send, status=404, payload={"error": "not found"})
                return
            if method != "POST":
                await self._json_response(
                    send,
                    status=405,
                    payload={"error": "method not allowed"},
                )
                return
            await self._handle_acknowledge_notification(
                notification_id=notification_id,
                send=send,
            )
            return

        session_prefix = "/api/sessions/"
        if path.startswith(session_prefix):
            session_path = path.removeprefix(session_prefix)
            is_approval_route = session_path.endswith("/approval")
            is_question_route = session_path.endswith("/question")
            is_result_route = session_path.endswith("/result")
            session_id = (
                session_path.removesuffix("/approval")
                if is_approval_route
                else session_path.removesuffix("/question")
                if is_question_route
                else session_path.removesuffix("/result")
                if is_result_route
                else session_path
            )
            try:
                validate_session_id(session_id)
            except ValueError:
                await self._json_response(
                    send,
                    status=404,
                    payload={"error": "not found"},
                )
                return
            if is_approval_route:
                if method != "POST":
                    await self._json_response(
                        send,
                        status=405,
                        payload={"error": "method not allowed"},
                    )
                    return
                await self._handle_approval_resolution(
                    session_id=session_id,
                    receive=receive,
                    send=send,
                )
                return
            if is_question_route:
                if method != "POST":
                    await self._json_response(
                        send,
                        status=405,
                        payload={"error": "method not allowed"},
                    )
                    return
                await self._handle_question_answer(
                    session_id=session_id,
                    receive=receive,
                    send=send,
                )
                return
            if is_result_route:
                if method != "GET":
                    await self._json_response(
                        send,
                        status=405,
                        payload={"error": "method not allowed"},
                    )
                    return
                await self._handle_session_result(session_id=session_id, send=send)
                return
            if method != "GET":
                await self._json_response(
                    send,
                    status=405,
                    payload={"error": "method not allowed"},
                )
                return
            await self._handle_resume(session_id=session_id, send=send)
            return

        await self._json_response(send, status=404, payload={"error": "not found"})

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
                continue
            if message_type == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
            if message_type == "lifespan.disconnect":
                return
            raise RuntimeError(f"unsupported lifespan message type: {message_type!r}")

    async def _handle_run_stream(self, receive: Receive, send: Send) -> None:
        try:
            body = await self._read_body(receive)
            request = self._parse_runtime_request(body)
        except ValueError as exc:
            await self._json_response(send, status=400, payload={"error": str(exc)})
            return

        runtime = self._runtime_factory()
        stream = self._stream_runtime_chunks(runtime, request)
        try:
            first_chunk = await anext(stream)
        except StopAsyncIteration:
            logger.exception("runtime stream emitted no chunks before response start")
            await self._json_response(send, status=500, payload={"error": "internal server error"})
            self._close_runtime(runtime)
            return
        except RuntimeRequestError as exc:
            await self._json_response(send, status=400, payload={"error": str(exc)})
            self._close_runtime(runtime)
            return
        except Exception:
            logger.exception("unexpected transport streaming failure")
            await self._json_response(send, status=500, payload={"error": "internal server error"})
            self._close_runtime(runtime)
            return

        await self._send_stream_start(send)

        emitted_failed_chunk = await self._send_runtime_stream_chunk(send, first_chunk)
        try:
            async for chunk in stream:
                chunk_failed = await self._send_runtime_stream_chunk(send, chunk)
                emitted_failed_chunk = emitted_failed_chunk or chunk_failed
        except Exception:
            if not emitted_failed_chunk:
                logger.exception("unexpected transport streaming failure")
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            self._close_runtime(runtime)
            return

        await send({"type": "http.response.body", "body": b"", "more_body": False})
        self._close_runtime(runtime)

    async def _send_stream_start(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream; charset=utf-8"),
                    (b"cache-control", b"no-cache"),
                ],
            }
        )

    async def _send_runtime_stream_chunk(
        self,
        send: Send,
        chunk: RuntimeStreamChunk,
    ) -> bool:
        payload = self._serialize_runtime_stream_chunk(chunk)
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        await send(
            {
                "type": "http.response.body",
                "body": b"data: " + data + b"\n\n",
                "more_body": True,
            }
        )
        return chunk.event is not None and chunk.event.event_type == "runtime.failed"

    async def _handle_list_sessions(self, send: Send) -> None:
        runtime = self._runtime_factory()
        try:
            payload = [
                self._serialize_stored_session_summary(item) for item in runtime.list_sessions()
            ]
        finally:
            self._close_runtime(runtime)
        await self._json_response(send, status=200, payload=payload)

    async def _handle_list_notifications(self, send: Send) -> None:
        runtime = self._runtime_factory()
        try:
            payload = [self._serialize_notification(item) for item in runtime.list_notifications()]
        finally:
            self._close_runtime(runtime)
        await self._json_response(send, status=200, payload=payload)

    async def _handle_get_settings(self, send: Send) -> None:
        runtime = self._runtime_factory()
        try:
            payload = runtime.web_settings()
        finally:
            self._close_runtime(runtime)
        await self._json_response(send, status=200, payload=payload)

    async def _handle_update_settings(self, receive: Receive, send: Send) -> None:
        try:
            body = await self._read_body(receive)
            payload = self._parse_settings_request(body)
        except ValueError as exc:
            await self._json_response(send, status=400, payload={"error": str(exc)})
            return

        runtime = self._runtime_factory()
        try:
            result = runtime.update_web_settings(**payload)
        except ValueError as exc:
            await self._json_response(send, status=400, payload={"error": str(exc)})
            return
        finally:
            self._close_runtime(runtime)
        await self._json_response(send, status=200, payload=result)

    async def _handle_resume(self, *, session_id: str, send: Send) -> None:
        runtime = self._runtime_factory()
        try:
            response = runtime.resume(session_id)
        except ValueError as exc:
            await self._json_response(send, status=404, payload={"error": str(exc)})
            return
        finally:
            self._close_runtime(runtime)
        await self._json_response(
            send,
            status=200,
            payload=self._serialize_runtime_response(response),
        )

    async def _handle_session_result(self, *, session_id: str, send: Send) -> None:
        runtime = self._runtime_factory()
        try:
            result = runtime.session_result(session_id=session_id)
        except ValueError as exc:
            await self._json_response(send, status=404, payload={"error": str(exc)})
            return
        finally:
            self._close_runtime(runtime)
        await self._json_response(
            send,
            status=200,
            payload=self._serialize_session_result(result),
        )

    async def _handle_approval_resolution(
        self,
        *,
        session_id: str,
        receive: Receive,
        send: Send,
    ) -> None:
        try:
            body = await self._read_body(receive)
            approval_request_id, approval_decision = self._parse_approval_resolution_request(body)
        except ValueError as exc:
            await self._json_response(send, status=400, payload={"error": str(exc)})
            return

        runtime = self._runtime_factory()
        try:
            response = runtime.resume(
                session_id,
                approval_request_id=approval_request_id,
                approval_decision=approval_decision,
            )
        except ValueError as exc:
            await self._json_response(send, status=404, payload={"error": str(exc)})
            return
        finally:
            self._close_runtime(runtime)
        await self._json_response(
            send,
            status=200,
            payload=self._serialize_runtime_response(response),
        )

    async def _handle_question_answer(
        self,
        *,
        session_id: str,
        receive: Receive,
        send: Send,
    ) -> None:
        try:
            body = await self._read_body(receive)
            question_request_id, responses = self._parse_question_answer_request(body)
        except ValueError as exc:
            await self._json_response(send, status=400, payload={"error": str(exc)})
            return

        runtime = self._runtime_factory()
        try:
            response = runtime.answer_question(
                session_id,
                question_request_id=question_request_id,
                responses=responses,
            )
        except (ValueError, NoPendingQuestionError) as exc:
            await self._json_response(send, status=404, payload={"error": str(exc)})
            return
        finally:
            self._close_runtime(runtime)
        await self._json_response(
            send,
            status=200,
            payload=self._serialize_runtime_response(response),
        )

    async def _handle_acknowledge_notification(
        self,
        *,
        notification_id: str,
        send: Send,
    ) -> None:
        runtime = self._runtime_factory()
        try:
            notification = runtime.acknowledge_notification(notification_id=notification_id)
        except ValueError as exc:
            await self._json_response(send, status=404, payload={"error": str(exc)})
            return
        finally:
            self._close_runtime(runtime)
        await self._json_response(
            send,
            status=200,
            payload=self._serialize_notification(notification),
        )

    async def _json_response(self, send: Send, *, status: int, payload: object) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _read_body(self, receive: Receive) -> bytes:
        body_parts: list[bytes] = []
        more_body = True

        while more_body:
            message = await receive()
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                raise ValueError("request body must be bytes")
            body_parts.append(body)
            more_body = bool(message.get("more_body", False))

        return b"".join(body_parts)

    def _parse_runtime_request(self, body: bytes) -> RuntimeRequest:
        try:
            raw_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

        if not isinstance(raw_payload, dict):
            raise ValueError("request body must be a JSON object")
        payload = cast(dict[str, object], raw_payload)

        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt must be a non-empty string")

        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ValueError("session_id must be a string when provided")
        if session_id is not None:
            validate_session_id(session_id)

        parent_session_id = payload.get("parent_session_id")
        if parent_session_id is not None and not isinstance(parent_session_id, str):
            raise ValueError("parent_session_id must be a string when provided")
        if parent_session_id is not None:
            validate_session_reference_id(
                parent_session_id,
                field_name="parent_session_id",
            )

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object when provided")
        normalized_metadata = validate_runtime_request_metadata(cast(dict[str, object], metadata))

        return RuntimeRequest(
            prompt=prompt,
            session_id=session_id,
            parent_session_id=parent_session_id,
            metadata=normalized_metadata,
            allocate_session_id=session_id is None,
        )

    def _parse_approval_resolution_request(
        self,
        body: bytes,
    ) -> tuple[str, PermissionResolution]:
        try:
            raw_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

        if not isinstance(raw_payload, dict):
            raise ValueError("request body must be a JSON object")
        payload = cast(dict[str, object], raw_payload)

        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")

        decision = payload.get("decision")
        if decision not in ("allow", "deny"):
            raise ValueError("decision must be 'allow' or 'deny'")

        return request_id, decision

    def _parse_settings_request(self, body: bytes) -> dict[str, str | None]:
        try:
            raw_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

        if not isinstance(raw_payload, dict):
            raise ValueError("request body must be a JSON object")

        payload = cast(dict[str, object], raw_payload)
        allowed_keys = {"provider", "provider_api_key", "model"}
        unknown_keys = sorted(key for key in payload if key not in allowed_keys)
        if unknown_keys:
            raise ValueError(f"unsupported settings field(s): {', '.join(unknown_keys)}")

        def _optional_string(name: str) -> str | None:
            value = payload.get(name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string when provided")
            stripped = value.strip()
            return stripped or None

        return {
            "provider": _optional_string("provider"),
            "provider_api_key": _optional_string("provider_api_key"),
            "model": _optional_string("model"),
        }

    def _parse_question_answer_request(
        self,
        body: bytes,
    ) -> tuple[str, tuple[QuestionResponse, ...]]:
        try:
            raw_payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc

        if not isinstance(raw_payload, dict):
            raise ValueError("request body must be a JSON object")
        payload = cast(dict[str, object], raw_payload)

        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")

        raw_responses = payload.get("responses")
        if not isinstance(raw_responses, list) or not raw_responses:
            raise ValueError("responses must be a non-empty array")

        response_items = cast(list[object], raw_responses)
        parsed: list[QuestionResponse] = []
        for index, raw_response in enumerate(response_items):
            if not isinstance(raw_response, dict):
                raise ValueError(f"responses[{index}] must be an object")
            response_payload = cast(dict[str, object], raw_response)
            header = response_payload.get("header")
            if not isinstance(header, str) or not header.strip():
                raise ValueError(f"responses[{index}].header must be a non-empty string")
            raw_answers = response_payload.get("answers")
            if not isinstance(raw_answers, list) or not raw_answers:
                raise ValueError(f"responses[{index}].answers must be a non-empty array")
            answer_items = cast(list[object], raw_answers)
            answers: list[str] = []
            for answer_index, raw_answer in enumerate(answer_items):
                if not isinstance(raw_answer, str) or not raw_answer.strip():
                    raise ValueError(
                        f"responses[{index}].answers[{answer_index}] must be a non-empty string"
                    )
                answers.append(raw_answer)
            parsed.append(QuestionResponse(header=header, answers=tuple(answers)))
        return request_id, tuple(parsed)

    @staticmethod
    def _serialize_runtime_stream_chunk(chunk: RuntimeStreamChunk) -> dict[str, object]:
        return {
            "kind": chunk.kind,
            "session": RuntimeTransportApp._serialize_session_state(chunk.session),
            "event": RuntimeTransportApp._serialize_event(chunk.event),
            "output": chunk.output,
        }

    @staticmethod
    def _serialize_runtime_response(response: RuntimeResponse) -> dict[str, object]:
        return {
            "session": RuntimeTransportApp._serialize_session_state(response.session),
            "events": [RuntimeTransportApp._serialize_event(event) for event in response.events],
            "output": response.output,
        }

    @staticmethod
    def _serialize_stored_session_summary(summary: StoredSessionSummary) -> dict[str, object]:
        return {
            "session": RuntimeTransportApp._serialize_session_ref(summary.session),
            "status": summary.status,
            "turn": summary.turn,
            "prompt": summary.prompt,
            "updated_at": summary.updated_at,
        }

    @staticmethod
    def _serialize_session_result(result: RuntimeSessionResult) -> dict[str, object]:
        return {
            "session": RuntimeTransportApp._serialize_session_state(result.session),
            "prompt": result.prompt,
            "status": result.status,
            "summary": result.summary,
            "output": result.output,
            "error": result.error,
            "last_event_sequence": result.last_event_sequence,
            "transcript": [
                RuntimeTransportApp._serialize_event(event) for event in result.transcript
            ],
        }

    @staticmethod
    def _serialize_notification(notification: RuntimeNotification) -> dict[str, object]:
        return {
            "id": notification.id,
            "session": RuntimeTransportApp._serialize_session_ref(notification.session),
            "kind": notification.kind,
            "status": notification.status,
            "summary": notification.summary,
            "event_sequence": notification.event_sequence,
            "created_at": notification.created_at,
            "acknowledged_at": notification.acknowledged_at,
            "payload": notification.payload,
        }

    @staticmethod
    def _serialize_session_ref(session_ref: SessionRef) -> dict[str, object]:
        payload: dict[str, object] = {"id": session_ref.id}
        if session_ref.parent_id is not None:
            payload["parent_id"] = session_ref.parent_id
        return payload

    @staticmethod
    def _serialize_session_state(session: SessionState) -> dict[str, object]:
        return {
            "session": RuntimeTransportApp._serialize_session_ref(session.session),
            "status": session.status,
            "turn": session.turn,
            "metadata": session.metadata,
        }

    @staticmethod
    def _serialize_event(event: EventEnvelope | None) -> dict[str, object] | None:
        if event is None:
            return None
        return {
            "session_id": event.session_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "source": event.source,
            "payload": event.payload,
        }

    async def _stream_runtime_chunks(
        self,
        runtime: RuntimeTransport,
        request: RuntimeRequest,
    ) -> AsyncIterator[RuntimeStreamChunk]:
        chunk_queue: queue.Queue[object] = queue.Queue()
        sentinel = object()

        def _produce() -> None:
            try:
                for chunk in runtime.run_stream(request):
                    chunk_queue.put(chunk)
            except Exception as exc:
                chunk_queue.put(exc)
            finally:
                chunk_queue.put(sentinel)

        worker = threading.Thread(target=_produce, name="runtime-stream-worker", daemon=True)
        worker.start()

        while True:
            item = await asyncio.to_thread(chunk_queue.get)
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise item
            yield cast(RuntimeStreamChunk, item)

        worker.join(timeout=0)


def create_runtime_app(
    *,
    workspace: Path,
    config: RuntimeConfig | None = None,
    runtime_factory: Callable[[], RuntimeTransport] | None = None,
) -> RuntimeTransportApp:
    resolved_factory = runtime_factory or (
        lambda: VoidCodeRuntime(workspace=workspace, config=config)
    )
    return RuntimeTransportApp(runtime_factory=resolved_factory)
