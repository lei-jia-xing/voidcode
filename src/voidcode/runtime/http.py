from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Protocol, cast, final

from .contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk, validate_session_id
from .events import EventEnvelope
from .service import VoidCodeRuntime
from .session import SessionRef, SessionState, StoredSessionSummary

logger = logging.getLogger(__name__)


class RuntimeTransport(Protocol):
    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]: ...

    def resume(self, session_id: str) -> RuntimeResponse: ...


class Receive(Protocol):
    async def __call__(self) -> dict[str, object]: ...


class Send(Protocol):
    async def __call__(self, message: dict[str, object]) -> None: ...


@final
class RuntimeTransportApp:
    _runtime_factory: Callable[[], RuntimeTransport]

    def __init__(self, *, runtime_factory: Callable[[], RuntimeTransport]) -> None:
        self._runtime_factory = runtime_factory

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

        session_prefix = "/api/sessions/"
        if path.startswith(session_prefix):
            if method != "GET":
                await self._json_response(
                    send,
                    status=405,
                    payload={"error": "method not allowed"},
                )
                return
            session_id = path.removeprefix(session_prefix)
            try:
                validate_session_id(session_id)
            except ValueError:
                await self._json_response(
                    send,
                    status=404,
                    payload={"error": "not found"},
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

        emitted_failed_chunk = False
        try:
            async for chunk in self._stream_runtime_chunks(runtime, request):
                payload = self._serialize_runtime_stream_chunk(chunk)
                emitted_failed_chunk = emitted_failed_chunk or (
                    chunk.event is not None and chunk.event.event_type == "runtime.failed"
                )
                data = json.dumps(payload, sort_keys=True).encode("utf-8")
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"data: " + data + b"\n\n",
                        "more_body": True,
                    }
                )
        except Exception:
            if not emitted_failed_chunk:
                logger.exception("unexpected transport streaming failure")
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _handle_list_sessions(self, send: Send) -> None:
        runtime = self._runtime_factory()
        payload = [self._serialize_stored_session_summary(item) for item in runtime.list_sessions()]
        await self._json_response(send, status=200, payload=payload)

    async def _handle_resume(self, *, session_id: str, send: Send) -> None:
        runtime = self._runtime_factory()
        try:
            response = runtime.resume(session_id)
        except ValueError as exc:
            await self._json_response(send, status=404, payload={"error": str(exc)})
            return
        await self._json_response(
            send,
            status=200,
            payload=self._serialize_runtime_response(response),
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

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object when provided")

        return RuntimeRequest(
            prompt=prompt,
            session_id=session_id,
            metadata=cast(dict[str, object], metadata),
            allocate_session_id=session_id is None,
        )

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
    def _serialize_session_ref(session_ref: SessionRef) -> dict[str, object]:
        return {"id": session_ref.id}

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
    runtime_factory: Callable[[], RuntimeTransport] | None = None,
) -> RuntimeTransportApp:
    resolved_factory = runtime_factory or (lambda: VoidCodeRuntime(workspace=workspace))
    return RuntimeTransportApp(runtime_factory=resolved_factory)
