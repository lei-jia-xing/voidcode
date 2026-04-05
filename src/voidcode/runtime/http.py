from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol, cast, final

from .contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk
from .events import EventEnvelope
from .service import VoidCodeRuntime
from .session import SessionRef, SessionState, StoredSessionSummary


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
        if scope.get("type") != "http":
            await self._json_response(
                send,
                status=404,
                payload={"error": "not found"},
            )
            return

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
            if not session_id or "/" in session_id:
                await self._json_response(
                    send,
                    status=404,
                    payload={"error": "not found"},
                )
                return
            await self._handle_resume(session_id=session_id, send=send)
            return

        await self._json_response(send, status=404, payload={"error": "not found"})

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

        try:
            for chunk in runtime.run_stream(request):
                payload = self._serialize_runtime_stream_chunk(chunk)
                data = json.dumps(payload, sort_keys=True).encode("utf-8")
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"data: " + data + b"\n\n",
                        "more_body": True,
                    }
                )
        except Exception:
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

        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object when provided")

        return RuntimeRequest(
            prompt=prompt,
            session_id=session_id,
            metadata=cast(dict[str, object], metadata),
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


def create_runtime_app(
    *,
    workspace: Path,
    runtime_factory: Callable[[], RuntimeTransport] | None = None,
) -> RuntimeTransportApp:
    resolved_factory = runtime_factory or (lambda: VoidCodeRuntime(workspace=workspace))
    return RuntimeTransportApp(runtime_factory=resolved_factory)
