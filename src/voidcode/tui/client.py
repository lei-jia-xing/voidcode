"""Multiplexed WebSocket JSON-RPC client implementing SessionClient."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import threading
import urllib.parse
from collections.abc import AsyncGenerator, Mapping
from typing import Any, Literal
from uuid import uuid4

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect
from websockets.typing import Origin, Subprotocol

from voidcode.sdk.client import (
    ApprovalPolicyListReceipt,
    ApprovalPolicyRevokeReceipt,
    ApprovalPolicyRule,
    ArtifactPageWire,
    CancelReceipt,
    CommandCatalog,
    CommandResult,
    ContinuationResult,
    CoreClientError,
    ErrorFrame,
    EventFrame,
    FollowupListReceipt,
    FollowupWithdrawReceipt,
    HeadFrame,
    HealthStatus,
    ResumeSpec,
    RunResult,
    RunSpec,
    SessionClient,
    SessionFrame,
    SessionRow,
    SessionUsageReport,
    StreamCancellation,
    ViewsSnapshot,
    parse_session_frame,
)
from voidcode.sdk.commands import CommandInputSnapshot
from voidcode.sdk.services import ArtifactPage
from voidcode.sdk.transcript import ContextHistory, TranscriptPage, decode_context_history, decode_transcript_page
from voidcode.transport.models import RPC_MAX_MESSAGE_BYTES, RPC_QUEUE_MESSAGES, RPC_SUBPROTOCOL, RpcFailure, RpcNotification, RpcSuccess

_END = object()


class CoreRpcClient(SessionClient):
    """One reconnectable connection; lost operations are never transparently retried."""

    def __init__(self, base_url: str, *, token: str | None = None) -> None:
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
            raise ValueError("server URL must use http(s) or ws(s)")
        http_scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
        self.base_url = urllib.parse.urlunsplit((http_scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.origin = f"{http_scheme}://{parsed.netloc}"
        self.token = token
        ws_scheme = "wss" if http_scheme == "https" else "ws"
        self._url = urllib.parse.urlunsplit((ws_scheme, parsed.netloc, parsed.path.rstrip("/") + "/api/rpc", "", ""))
        self._lock = threading.RLock()
        self._socket: ClientConnection | None = None
        self._reader_thread: threading.Thread | None = None
        self._pending: dict[str, concurrent.futures.Future[Any]] = {}
        self._streams: dict[str, queue.Queue[Any]] = {}
        self._closed = False

    def __enter__(self) -> CoreRpcClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            socket = self._socket
            worker = self._reader_thread
        if socket is not None:
            socket.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5)

    def _connect(self) -> ClientConnection:
        # Caller owns _lock, including registration of the first pending call.
        if self._closed:
            raise CoreClientError("RPC client is closed")
        if self._socket is not None:
            return self._socket
        socket: ClientConnection | None = None
        try:
            socket = connect(
                self._url,
                origin=Origin(self.origin),
                subprotocols=[Subprotocol(RPC_SUBPROTOCOL)],
                proxy=None,
                max_size=RPC_MAX_MESSAGE_BYTES,
                max_queue=RPC_QUEUE_MESSAGES,
                close_timeout=2,
            )
            identity = uuid4().hex
            socket.send(json.dumps({"jsonrpc": "2.0", "id": identity, "method": "connection.authenticate", "params": {"token": self.token}}))
            raw = json.loads(socket.recv(timeout=10))
            if "error" in raw:
                error = RpcFailure.model_validate(raw).error
                socket.close()
                raise CoreClientError(error.message, status_code=int((error.data or {}).get("status", 0)), code=(error.data or {}).get("code"))
            response = RpcSuccess.model_validate(raw)
            if response.id != identity or response.result != {"protocol_version": 1}:
                socket.close()
                raise CoreClientError("malformed RPC authentication response")
        except CoreClientError:
            raise
        except Exception as exc:
            if socket is not None:
                socket.close()
            raise CoreClientError(f"RPC connection failed: {type(exc).__name__}") from exc
        self._socket = socket
        self._reader_thread = threading.Thread(target=self._reader, args=(socket,), name="voidcode-rpc-reader", daemon=True)
        self._reader_thread.start()
        return socket

    def _reader(self, socket: ClientConnection) -> None:
        error = CoreClientError(
            "RPC connection lost; operation outcome may be unknown; inspect session history before retrying", code="outcome_unknown"
        )
        try:
            for text in socket:
                if not isinstance(text, str):
                    raise ValueError("JSON-RPC requires text messages")
                raw = json.loads(text)
                if isinstance(raw, dict) and "method" in raw:
                    notice = RpcNotification.model_validate(raw)
                    identity = notice.params.get("subscription_id")
                    with self._lock:
                        sink = self._streams.get(identity)
                    if sink is not None:
                        if notice.method == "session.frame":
                            frame = parse_session_frame(notice.params["frame"])
                            sink.put_nowait(frame)
                        elif notice.method == "subscription.closed":
                            sink.put_nowait(_END)
                        else:
                            raise ValueError("unknown RPC notification")
                else:
                    response = RpcFailure.model_validate(raw) if isinstance(raw, dict) and "error" in raw else RpcSuccess.model_validate(raw)
                    with self._lock:
                        future = self._pending.pop(str(response.id), None)
                    if future is not None:
                        if isinstance(response, RpcFailure):
                            data = response.error.data or {}
                            future.set_exception(
                                CoreClientError(response.error.message, status_code=int(data.get("status", 0)), code=data.get("code"))
                            )
                        else:
                            future.set_result(response.result)
        except ValueError as exc:
            error = CoreClientError(f"malformed RPC message: {type(exc).__name__}", status_code=400, code="protocol_error")
        except queue.Full:
            error = CoreClientError("RPC event queue overflow; reconnect from applied cursor", code="stream_overflow")
        except Exception as exc:
            if not self._closed:
                error = CoreClientError(f"RPC connection lost ({type(exc).__name__}); operation outcome may be unknown", code="outcome_unknown")
        finally:
            socket.close()
            with self._lock:
                if self._socket is socket:
                    self._socket = None
                    for future in self._pending.values():
                        if not future.done():
                            future.set_exception(error)
                    self._pending.clear()
                    for sink in self._streams.values():
                        while not sink.empty():
                            try:
                                sink.get_nowait()
                            except queue.Empty:
                                break
                        sink.put_nowait(error)

    def _call(self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float = 300) -> dict[str, Any]:
        identity = uuid4().hex
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        with self._lock:
            socket = self._connect()
            self._pending[identity] = future
            try:
                socket.send(json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": dict(params or {})}, allow_nan=False))
            except Exception as exc:
                self._pending.pop(identity, None)
                raise CoreClientError("RPC send failed; operation outcome may be unknown", code="outcome_unknown") from exc
        try:
            result = future.result(timeout=timeout)
        except TimeoutError as exc:
            with self._lock:
                self._pending.pop(identity, None)
            raise CoreClientError("RPC response timed out; operation outcome may be unknown", code="outcome_unknown") from exc
        if not isinstance(result, dict):
            raise CoreClientError("RPC result must be an object")
        return result

    def health(self) -> HealthStatus:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = httpx.get(self.base_url + "/api/health", headers=headers, timeout=10, trust_env=False)
            if response.is_error:
                raise CoreClientError.from_refusal(response.json(), status_code=response.status_code)
            return HealthStatus.from_payload(response.json())
        except httpx.RequestError as exc:
            raise CoreClientError(f"health connection failed: {type(exc).__name__}") from exc

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog.from_payload(self._call("command.catalog"))

    def list_sessions(self, limit: int = 100) -> list[SessionRow]:
        return [SessionRow.from_payload(row) for row in self._call("session.list", {"limit": limit})["sessions"]]

    def create_session(self, session_id: str | None = None) -> SessionRow:
        return SessionRow.from_payload(self._call("session.create", {"session_id": session_id}))

    def get_session(self, session_id: str) -> SessionRow:
        return SessionRow.from_payload(self._call("session.get", {"session_id": session_id}))

    def show(self, session_id: str, events: bool = False) -> Mapping[str, Any]:
        return self._call("session.show", {"session_id": session_id, "events": events})

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        payload = self._call("artifact.read", {"sha256": sha256, "offset": offset, "limit": limit})
        try:
            wire = ArtifactPageWire.from_payload(payload)
        except (TypeError, ValueError) as exc:
            raise CoreClientError("malformed artifact page response", status_code=502, code="protocol_error") from exc
        page = wire.as_page()
        if page.artifact.sha256 != sha256 or page.offset != offset or len(page.data) > limit:
            raise CoreClientError("malformed artifact page response", status_code=502, code="protocol_error")
        return page

    def run(self, session_id: str, spec: RunSpec) -> RunResult:
        params: dict[str, object] = {"session_id": session_id, "prompt": spec.prompt}
        if spec.max_steps is not None:
            params["max_steps"] = spec.max_steps
        if spec.root_action_budget is not None:
            params["root_action_budget"] = spec.root_action_budget
        if spec.interaction is not None:
            params["interaction"] = spec.interaction
        if spec.images:
            params["images"] = [img.as_payload() for img in spec.images]
        return RunResult.from_payload(self._call("session.run", params))

    def resume(self, session_id: str, spec: ResumeSpec | None = None) -> RunResult:
        active = spec or ResumeSpec()
        params: dict[str, object] = {"session_id": session_id}
        if active.approval_request_id is not None:
            params["approval_request_id"] = active.approval_request_id
            params["decision"] = active.decision
        if active.max_steps is not None:
            params["max_steps"] = active.max_steps
        if active.extend_root_actions is not None:
            params["extend_root_actions"] = active.extend_root_actions
            params["root_action_grant_id"] = active.root_action_grant_id
        if active.interaction is not None:
            params["interaction"] = active.interaction
        if active.reason is not None:
            params["reason"] = active.reason
        if active.grant_session_policy:
            params["grant_session_policy"] = active.grant_session_policy
        return RunResult.from_payload(self._call("session.resume", params))

    def answer_ask(
        self,
        session_id: str,
        request_id: str,
        values: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> RunResult:
        payload = {
            "session_id": session_id,
            "request_id": request_id,
            "values": dict(values),
            "expected_revision": expected_revision,
        }
        return RunResult.from_payload(self._call("ask.answer", payload))

    def cancel_ask(
        self,
        session_id: str,
        request_id: str,
        *,
        expected_revision: str,
    ) -> RunResult:
        payload = {
            "session_id": session_id,
            "request_id": request_id,
            "expected_revision": expected_revision,
        }
        return RunResult.from_payload(self._call("ask.cancel", payload))

    def list_approval_policies(self, session_id: str) -> list[ApprovalPolicyRule]:
        body = self._call("approval_policy.list", {"session_id": session_id})
        return list(ApprovalPolicyListReceipt.from_payload(body).policies)

    def revoke_approval_policy(self, session_id: str, rule_id: str) -> bool:
        body = self._call("approval_policy.revoke", {"session_id": session_id, "rule_id": rule_id})
        return bool(ApprovalPolicyRevokeReceipt.from_payload(body).revoked)

    def command_input(
        self, session_id: str, name: str, expected_revision: str, *, arguments: Mapping[str, object] | None = None
    ) -> CommandInputSnapshot:
        return CommandInputSnapshot.from_payload(
            self._call(
                "command.input",
                {
                    "session_id": session_id,
                    "name": name,
                    "arguments": dict(arguments or {}),
                    "expected_revision": expected_revision,
                },
            )
        )

    def invoke_command(self, session_id: str, name: str, arguments: Mapping[str, object] | None = None, expected_revision: str = "") -> CommandResult:
        return CommandResult.from_payload(
            self._call(
                "command.invoke",
                {
                    "session_id": session_id,
                    "name": name,
                    "arguments": dict(arguments or {}),
                    "expected_revision": expected_revision,
                },
            )
        )

    def confirm(self, session_id: str, request_id: str, decision: Literal["allow", "deny"], expected_revision: str = "") -> CommandResult:
        return CommandResult.from_payload(
            self._call(
                "action.confirm",
                {
                    "session_id": session_id,
                    "request_id": request_id,
                    "decision": decision,
                    "expected_revision": expected_revision,
                },
            )
        )

    def views(self, session_id: str) -> ViewsSnapshot:
        return ViewsSnapshot.from_payload(self._call("session.views", {"session_id": session_id}))

    def drain(self, session_id: str, *, limit: int = 10) -> list[ContinuationResult]:
        return [
            ContinuationResult.from_payload(row) for row in self._call("session.drain", {"session_id": session_id, "limit": limit})["continuations"]
        ]

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        return SessionUsageReport.from_payload(
            self._call(
                "session.usage",
                {
                    "session_id": session_id,
                    "since_sequence": since_sequence,
                    "until_sequence": until_sequence,
                },
            )
        )

    def cancel(self, session_id: str) -> CancelReceipt:
        return CancelReceipt.from_payload(self._call("session.cancel", {"session_id": session_id}))

    def list_followups(self, session_id: str) -> FollowupListReceipt:
        return FollowupListReceipt.from_payload(self._call("followup.list", {"session_id": session_id}))

    def withdraw_followup(self, session_id: str, continuation_id: str, expected_revision: str = "") -> FollowupWithdrawReceipt:
        return FollowupWithdrawReceipt.from_payload(
            self._call(
                "followup.withdraw",
                {
                    "session_id": session_id,
                    "continuation_id": continuation_id,
                    "expected_revision": expected_revision,
                },
            )
        )

    def transcript(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        return decode_transcript_page(self._call("session.transcript", {"session_id": session_id, "after_sequence": after_sequence, "limit": limit}))

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory:
        return decode_context_history(self._call("session.context", {"session_id": session_id, "limit": limit}))

    async def events_stream(
        self, session_id: str, after_sequence: int = 0, *, follow: bool = True, cancel: StreamCancellation | None = None
    ) -> AsyncGenerator[SessionFrame]:
        identity = uuid4().hex
        sink: queue.Queue[Any] = queue.Queue(maxsize=RPC_QUEUE_MESSAGES)
        if cancel is not None:
            task = asyncio.current_task()
            if task is not None:
                cancel.attach(asyncio.get_running_loop(), task)
        subscribed = False
        try:
            if cancel is not None and cancel.is_closed:
                return
            with self._lock:
                self._streams[identity] = sink
            subscribed = True
            await asyncio.to_thread(
                self._call,
                "session.subscribe",
                {
                    "subscription_id": identity,
                    "session_id": session_id,
                    "after_sequence": after_sequence,
                    "follow": follow,
                },
            )
            while True:
                try:
                    frame = await asyncio.to_thread(sink.get, True, 0.2)
                except queue.Empty:
                    continue
                if frame is _END:
                    return
                if isinstance(frame, Exception):
                    raise frame
                yield frame
                cursor = (
                    max((event.sequence for event in frame.events), default=after_sequence)
                    if isinstance(frame, HeadFrame)
                    else (frame.event.sequence if isinstance(frame, EventFrame) else None)
                )
                if cursor is not None:
                    with self._lock:
                        socket = self._socket
                        if socket is not None:
                            try:
                                socket.send(
                                    json.dumps(
                                        {
                                            "jsonrpc": "2.0",
                                            "method": "session.ack",
                                            "params": {
                                                "subscription_id": identity,
                                                "through_sequence": cursor,
                                            },
                                        }
                                    )
                                )
                            except ConnectionClosed:
                                # Acks are best-effort: a concurrent close ends the
                                # stream through the reader's error frame, not here.
                                pass
                if isinstance(frame, ErrorFrame):
                    return
        except asyncio.CancelledError:
            return
        finally:
            with self._lock:
                self._streams.pop(identity, None)
                connected = self._socket is not None
            if subscribed and connected and not self._closed:
                try:
                    await asyncio.to_thread(self._call, "session.unsubscribe", {"subscription_id": identity}, timeout=5)
                except CoreClientError, asyncio.CancelledError:
                    pass
            if cancel is not None:
                cancel.detach()
