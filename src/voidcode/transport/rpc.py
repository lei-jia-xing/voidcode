"""Concurrent JSON-RPC dispatch and bounded event subscriptions over WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from voidcode.capabilities.files.path_queries import PATH_COMPLETION_QUERY
from voidcode.core.engine import Core
from voidcode.core.storage.store import SessionRecord
from voidcode.sdk.client import (
    ApprovalPolicyListReceipt,
    ApprovalPolicyRevokeReceipt,
    ArtifactPageWire,
    CancelReceipt,
    FollowupListReceipt,
    FollowupWithdrawReceipt,
    RunResult,
    SessionRow,
)
from voidcode.sdk.commands import CommandCatalog, CommandInputSnapshot, CommandResult
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ImageAttachment
from voidcode.sdk.services import SessionUsageReport
from voidcode.sdk.transcript import ContextHistoryWire, TranscriptPageWire, encode_context_history, encode_transcript_page
from voidcode.sdk.views import ViewsSnapshot

from . import models as m
from .models import (
    RPC_EVENT_WINDOW,
    RPC_MAX_MESSAGE_BYTES,
    RPC_MAX_PENDING,
    RPC_MAX_SUBSCRIPTIONS,
    RPC_QUEUE_BYTES,
    RPC_QUEUE_MESSAGES,
    RpcModel,
    RpcRequest,
)
from .projection import FOLLOW_TAIL_LIMIT, core_error_status, event_payload, safe_projection_error, session_payload

logger = logging.getLogger(__name__)

#: Idle wake cadence for follow subscriptions: closed/cancel recheck only. Commits
#: notify immediately; this timer never drives data (and backstops missed wakes).
_FOLLOW_WAIT_TIMEOUT = 5.0


@dataclass(frozen=True)
class RpcMethod:
    params: type[RpcModel]
    result: Any
    invoke: Callable[[Core, Any], object]


def _project_session(core: Core, record: SessionRecord) -> dict[str, object]:
    summary, created_at, updated_at = core.store.session_meta(record.session_id)
    return session_payload(record, summary=summary, created_at=created_at, updated_at=updated_at)


METHODS: dict[str, RpcMethod] = {
    "artifact.read": RpcMethod(
        m.ArtifactReadParams,
        ArtifactPageWire,
        lambda c, p: ArtifactPageWire.from_page(c.read_artifact(p.sha256, offset=p.offset, limit=p.limit)).model_dump(mode="json"),
    ),
    "command.catalog": RpcMethod(m.EmptyParams, CommandCatalog, lambda c, _p: c.command_catalog().as_payload()),
    "session.list": RpcMethod(
        m.ListSessionsParams, m.SessionsBody, lambda c, p: {"sessions": [_project_session(c, s) for s in c.sessions(limit=p.limit)]}
    ),
    "session.create": RpcMethod(m.CreateSessionRequest, SessionRow, lambda c, p: _project_session(c, c.create_session(p.session_id))),
    "session.get": RpcMethod(m.SessionParams, SessionRow, lambda c, p: _project_session(c, c.session(p.session_id))),
    "session.show": RpcMethod(m.ShowParams, m.ShowBody, lambda c, p: c.show(p.session_id, events=p.events)),
    "session.run": RpcMethod(
        m.RunRequest,
        RunResult,
        lambda c, p: c.run(
            p.prompt,
            session_id=p.session_id,
            max_steps=p.max_steps,
            root_action_budget=p.root_action_budget,
            images=tuple(ImageAttachment.from_payload(image.model_dump(exclude_none=True)) for image in p.images),
            interaction=p.interaction,
        ).as_payload(),
    ),
    "session.resume": RpcMethod(
        m.ResumeRequest,
        RunResult,
        lambda c, p: c.resume(
            p.session_id,
            approval_request_id=p.approval_request_id,
            decision=p.decision,
            max_steps=p.max_steps,
            extend_root_actions=p.extend_root_actions,
            root_action_grant_id=p.root_action_grant_id,
            interaction=p.interaction,
            reason=p.reason,
            grant_session_policy=p.grant_session_policy,
        ).as_payload(),
    ),
    "approval_policy.list": RpcMethod(
        m.SessionParams,
        ApprovalPolicyListReceipt,
        lambda c, p: {"policies": [r.as_payload() for r in c.list_approval_policies(p.session_id)]},
    ),
    "approval_policy.revoke": RpcMethod(
        m.ApprovalPolicyRevokeRequest,
        ApprovalPolicyRevokeReceipt,
        lambda c, p: {"revoked": c.revoke_approval_policy(p.session_id, p.rule_id)},
    ),
    "command.input": RpcMethod(
        m.CommandInputRequest,
        CommandInputSnapshot,
        lambda c, p: c.command_input(p.session_id, p.name, arguments=p.arguments, expected_revision=p.expected_revision).as_payload(),
    ),
    "command.invoke": RpcMethod(
        m.InvokeRequest,
        CommandResult,
        lambda c, p: c.invoke_command(p.session_id, p.name, p.arguments, expected_revision=p.expected_revision).as_payload(),
    ),
    "workspace.complete_paths": RpcMethod(
        m.CompletePathsParams,
        m.CompletePathsBody,
        lambda c, p: {
            "answer": None
            if (answer := c.plugin_query(p.session_id, PATH_COMPLETION_QUERY, p.model_dump(exclude={"session_id"}))) is None
            else json_wire_object(answer)
        },
    ),
    "action.confirm": RpcMethod(
        m.ConfirmRequest,
        CommandResult,
        lambda c, p: c.confirm_action(p.session_id, p.request_id, p.decision, expected_revision=p.expected_revision).as_payload(),
    ),
    "ask.answer": RpcMethod(
        m.AskAnswerRequest,
        RunResult,
        lambda c, p: c.answer_ask(p.session_id, p.request_id, p.values, expected_revision=p.expected_revision).as_payload(),
    ),
    "ask.cancel": RpcMethod(
        m.AskCancelRequest,
        RunResult,
        lambda c, p: c.cancel_ask(p.session_id, p.request_id, expected_revision=p.expected_revision).as_payload(),
    ),
    "session.views": RpcMethod(m.SessionParams, ViewsSnapshot, lambda c, p: c.plugin_views(p.session_id).as_payload()),
    "session.drain": RpcMethod(
        m.DrainRequest,
        m.ContinuationsBody,
        lambda c, p: {"continuations": [r.as_payload() for r in c.drain_continuations(p.session_id, limit=p.limit)]},
    ),
    "session.usage": RpcMethod(
        m.UsageParams,
        SessionUsageReport,
        lambda c, p: c.usage(p.session_id, since_sequence=p.since_sequence, until_sequence=p.until_sequence).as_payload(),
    ),
    "session.transcript": RpcMethod(
        m.TranscriptParams,
        TranscriptPageWire,
        lambda c, p: encode_transcript_page(c.transcript_page(p.session_id, after_sequence=p.after_sequence, limit=p.limit)),
    ),
    "session.context": RpcMethod(
        m.ContextParams, ContextHistoryWire, lambda c, p: encode_context_history(c.context_history(p.session_id, limit=p.limit))
    ),
    "session.cancel": RpcMethod(
        m.SessionParams,
        CancelReceipt,
        lambda c, p: {"session_id": p.session_id, "cancellation_requested": c.cancel_session(p.session_id, reason="RPC session cancellation")},
    ),
    "followup.list": RpcMethod(m.SessionParams, FollowupListReceipt, lambda c, p: c.list_followups(p.session_id).as_payload()),
    "followup.withdraw": RpcMethod(
        m.FollowupWithdrawRequest,
        FollowupWithdrawReceipt,
        lambda c, p: c.withdraw_followup(p.session_id, p.continuation_id, expected_revision=p.expected_revision).as_payload(),
    ),
}

# These methods belong to a connection rather than the Core authority.
CONNECTION_METHODS = {
    "connection.authenticate": (m.AuthenticateParams, m.AuthenticateReceipt),
    "session.subscribe": (m.SubscribeParams, m.SubscriptionReceipt),
    "session.unsubscribe": (m.UnsubscribeParams, m.UnsubscribeReceipt),
    "session.ack": (m.AckParams, m.AckReceipt),
}


def failure(identity: str | int | None, code: int, message: str, *, data: dict[str, object] | None = None) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": identity, "error": error}


def parse_json(text: str) -> Any:
    def invalid_constant(value: str) -> Any:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(text, parse_constant=invalid_constant)


@dataclass
class SubscriptionWindow:
    sent: int
    acknowledged: int
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class RpcConnection:
    def __init__(self, socket: WebSocket, core: Core, calls: set[asyncio.Task[None]]) -> None:
        self.socket = socket
        self.core = core
        self.calls = calls
        self.closed = False
        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue(maxsize=RPC_QUEUE_MESSAGES)
        self.queued_bytes = 0
        self.pending: set[str | int | None] = set()
        self.active = 0
        self.subscriptions: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}
        self.subscription_starts: dict[str, asyncio.Event] = {}
        self.windows: dict[str, SubscriptionWindow] = {}

    def enqueue(self, payload: object) -> None:
        if self.closed:
            return
        text = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        size = len(text.encode("utf-8"))
        if size > RPC_MAX_MESSAGE_BYTES or self.queue.full() or self.queued_bytes + size > RPC_QUEUE_BYTES:
            self.closed = True
            # Closing the connection forces consumers to resume from their applied
            # cursor. Never silently skip an event or an operation's response.
            task = asyncio.create_task(self.socket.close(code=1013, reason="outbound queue limit; reconnect from applied cursor"))
            self.calls.add(task)
            task.add_done_callback(self.calls.discard)
            return
        self.queue.put_nowait((text, size))
        self.queued_bytes += size

    async def _sender(self) -> None:
        while True:
            text, size = await self.queue.get()
            self.queued_bytes -= size
            await asyncio.wait_for(self.socket.send_text(text), timeout=10)

    async def serve(self) -> None:
        sender = asyncio.create_task(self._sender())
        receiver = asyncio.create_task(self._receive())
        try:
            await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.closed = True
            # Only connection-owned subscriptions and I/O are cancelled. Core calls
            # survive a lost connection and remain tracked by the application's lifespan.
            tasks = [sender, receiver, *(task for task, _ready in self.subscriptions.values())]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.subscriptions.clear()

    async def _receive(self) -> None:
        try:
            while not self.closed:
                message = await self.socket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if text is None:
                    await self.socket.close(code=1003, reason="JSON-RPC requires text messages")
                    return
                if len(text.encode("utf-8")) > RPC_MAX_MESSAGE_BYTES:
                    await self.socket.close(code=1009, reason="message too large")
                    return
                try:
                    raw = parse_json(text)
                except ValueError, RecursionError:
                    self.enqueue(failure(None, -32700, "Parse error"))
                    continue
                if isinstance(raw, list) and (not raw or len(raw) > RPC_MAX_PENDING):
                    self.enqueue(failure(None, -32600, "Invalid Request: batch must contain 1..32 calls"))
                    continue
                # Bound tasks before spawning, while leaving room for a cancellation
                # or unsubscribe even when every ordinary call is occupied.
                count = len(raw) if isinstance(raw, list) else 1
                control = isinstance(raw, dict) and raw.get("method") in {"session.cancel", "session.unsubscribe", "session.ack"}
                if self.active + count > RPC_MAX_PENDING + (8 if control else 0):
                    self.enqueue(failure(None, -32003, "Too many pending calls"))
                    await self.socket.close(code=1013, reason="pending call limit")
                    return
                self.active += count
                task = asyncio.create_task(self._message(raw, count))
                self.calls.add(task)
                task.add_done_callback(self.calls.discard)
        except WebSocketDisconnect, RuntimeError:
            return

    async def _message(self, raw: Any, count: int) -> None:
        try:
            if isinstance(raw, list):
                replies = await asyncio.gather(*(self._execute(item) for item in raw))
                payload: object = [reply for reply in replies if reply is not None]
            else:
                payload = await self._execute(raw)
            if payload is not None and payload != []:
                self.enqueue(payload)
                # Subscription replay starts only AFTER its receipt is queued, including
                # when the receipt is one member of a JSON-RPC batch response.
                for reply in payload if isinstance(payload, list) else [payload]:
                    result = reply.get("result") if isinstance(reply, dict) else None
                    if isinstance(result, dict):
                        subscription = self.subscriptions.get(result.get("subscription_id"))
                        if subscription is not None:
                            subscription[1].set()
        except Exception:
            logger.exception("RPC response handling failed")
            self.enqueue(failure(None, -32603, "Internal error"))
        finally:
            self.active -= count

    async def _execute(self, raw: Any) -> dict[str, object] | None:
        try:
            request = RpcRequest.model_validate(raw)
        except ValidationError:
            return failure(None, -32600, "Invalid Request")
        notification = "id" not in request.model_fields_set
        identity = request.id
        if not notification and identity in self.pending:
            return failure(identity, -32600, "Request ID is already pending")
        if not notification:
            self.pending.add(identity)
        try:
            if request.method == "session.subscribe":
                params = m.SubscribeParams.model_validate(request.params)
                if notification:
                    return None
                result = await self._subscribe(params)
            elif request.method == "session.ack":
                params = m.AckParams.model_validate(request.params)
                window = self.windows.get(params.subscription_id)
                if window is None:
                    raise ValueError("unknown subscription")
                if not window.acknowledged <= params.through_sequence <= window.sent:
                    raise ValueError("acknowledgement must be monotonic and no later than delivered events")
                window.acknowledged = params.through_sequence
                window.changed.set()
                result = {"through_sequence": window.acknowledged}
            elif request.method == "session.unsubscribe":
                params = m.UnsubscribeParams.model_validate(request.params)
                starting = self.subscription_starts.get(params.subscription_id)
                if starting is not None:
                    starting.set()
                entry = self.subscriptions.pop(params.subscription_id, None)
                if entry:
                    entry[0].cancel()
                    await asyncio.gather(entry[0], return_exceptions=True)
                result = {"removed": entry is not None or starting is not None}
            else:
                method = METHODS.get(request.method)
                if method is None:
                    return None if notification else failure(identity, -32601, "Method not found")
                params = method.params.model_validate(request.params)
                result = await asyncio.to_thread(method.invoke, self.core, params)
            return None if notification else {"jsonrpc": "2.0", "id": identity, "result": result}
        except ValidationError as exc:
            first = exc.errors(include_input=False, include_context=False)[0]
            message = f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
            return None if notification else failure(identity, -32602, message, data={"code": "invalid_params", "status": 400})
        except Exception as exc:
            status, code = core_error_status(exc)
            message = str(exc) if status != 500 else "Internal error"
            return None if notification else failure(identity, -32603 if status == 500 else -32000, message, data={"code": code, "status": status})
        finally:
            if not notification:
                self.pending.discard(identity)

    async def _subscribe(self, params: m.SubscribeParams) -> dict[str, object]:
        if params.subscription_id in self.subscriptions or params.subscription_id in self.subscription_starts:
            raise ValueError("subscription_id is already active")
        if len(self.subscriptions) + len(self.subscription_starts) >= RPC_MAX_SUBSCRIPTIONS:
            raise ValueError("subscription limit reached")
        cancelled = asyncio.Event()
        self.subscription_starts[params.subscription_id] = cancelled
        try:
            record = await asyncio.to_thread(self.core.session, params.session_id)
            if self.closed or cancelled.is_set():
                raise ValueError("subscription cancelled before attachment")
        finally:
            self.subscription_starts.pop(params.subscription_id, None)
        ready = asyncio.Event()
        self.windows[params.subscription_id] = SubscriptionWindow(params.after_sequence, params.after_sequence)
        task = asyncio.create_task(self._events(params, _project_session(self.core, record), ready))
        self.subscriptions[params.subscription_id] = (task, ready)
        return {"subscription_id": params.subscription_id}

    async def _events(self, params: m.SubscribeParams, session: dict[str, object], ready: asyncio.Event) -> None:
        cursor = params.after_sequence
        tip = session["last_event_sequence"]
        assert isinstance(tip, int)
        first = True
        window = self.windows[params.subscription_id]
        generation = self.core.commit_generation
        try:
            await ready.wait()
            while not self.closed:
                credit = RPC_EVENT_WINDOW - (cursor - window.acknowledged)
                if credit <= 0:
                    window.changed.clear()
                    await window.changed.wait()
                    continue
                limit = min(FOLLOW_TAIL_LIMIT, credit)
                chunk = await asyncio.to_thread(self.core.events_after, params.session_id, after_sequence=cursor, limit=limit)
                if not params.follow:
                    chunk = tuple(event for event in chunk if event.sequence <= tip)
                frames = []
                selected = []
                page_bytes = 0
                for event in chunk:
                    try:
                        frame = event_payload(params.session_id, event)
                        size = len(json.dumps(frame, allow_nan=False).encode("utf-8"))
                        if size > RPC_MAX_MESSAGE_BYTES // 2:
                            self._frame(
                                params.subscription_id,
                                {
                                    "kind": "error",
                                    "session_id": params.session_id,
                                    "error": f"event exceeds RPC message limit: sequence={event.sequence}",
                                },
                            )
                            return
                        if first and frames and page_bytes + size > RPC_MAX_MESSAGE_BYTES // 2:
                            break
                        frames.append(frame)
                        selected.append(event)
                        page_bytes += size
                    except Exception as exc:
                        self._frame(
                            params.subscription_id, {"kind": "error", "session_id": params.session_id, "error": safe_projection_error(event, exc)}
                        )
                        return
                if selected:
                    window.sent = selected[-1].sequence
                if first:
                    self._frame(params.subscription_id, {"session": session, "events": frames})
                    first = False
                else:
                    for event in frames:
                        self._frame(params.subscription_id, {"kind": "event", "session_id": params.session_id, "event": event})
                        await asyncio.sleep(0)
                if selected:
                    cursor = selected[-1].sequence
                if len(selected) == len(chunk) and len(chunk) < limit:
                    if not params.follow:
                        return
                    generation = await asyncio.to_thread(self.core.wait_for_commit, generation, _FOLLOW_WAIT_TIMEOUT)
                else:
                    # Yield so the sender can flush bounded replay pages.
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._frame(
                params.subscription_id, {"kind": "error", "session_id": params.session_id, "error": f"event read failed: {type(exc).__name__}"}
            )
        finally:
            self.subscriptions.pop(params.subscription_id, None)
            self.windows.pop(params.subscription_id, None)
            self.enqueue({"jsonrpc": "2.0", "method": "subscription.closed", "params": {"subscription_id": params.subscription_id}})

    def _frame(self, subscription_id: str, frame: dict[str, object]) -> None:
        self.enqueue({"jsonrpc": "2.0", "method": "session.frame", "params": {"subscription_id": subscription_id, "frame": frame}})
