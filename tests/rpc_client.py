"""Raw JSON-RPC test peer; every call crosses a real WebSocket boundary."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from websockets.sync.client import connect
from websockets.typing import Origin, Subprotocol

from voidcode.transport.models import RPC_SUBPROTOCOL


def request(base: str, method: str, params: object = None, *, token: str | None = None, origin: str | None = None) -> tuple[int, bytes]:
    with connect(
        base.replace("http:", "ws:").replace("https:", "wss:") + "/api/rpc",
        origin=Origin(origin) if origin else None,
        subprotocols=[Subprotocol(RPC_SUBPROTOCOL)],
        proxy=None,
    ) as socket:
        socket.send(json.dumps({"jsonrpc": "2.0", "id": "auth", "method": "connection.authenticate", "params": {"token": token}}))
        auth = json.loads(socket.recv(timeout=30))
        if "error" in auth:
            return _refusal(auth["error"])
        identity = uuid4().hex
        payload: Any = {} if params is None else params
        if method == "session.subscribe" and isinstance(payload, dict):
            payload = {"subscription_id": identity, "follow": False, **payload}
        socket.send(json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": payload}))
        response = json.loads(socket.recv(timeout=30))
        if "error" in response:
            return _refusal(response["error"])
        if method == "session.subscribe":
            frames = []
            while True:
                notice = json.loads(socket.recv(timeout=30))
                if notice["method"] == "subscription.closed":
                    break
                frame = notice["params"]["frame"]
                frames.append(frame)
                cursor = (
                    max((event["sequence"] for event in frame["events"]), default=payload.get("after_sequence", 0))
                    if "events" in frame
                    else (frame["event"]["sequence"] if frame.get("kind") == "event" else None)
                )
                if cursor is not None:
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
            return 200, json.dumps(frames).encode()
        return 200, json.dumps(response["result"]).encode()


def _refusal(error: dict) -> tuple[int, bytes]:
    data = error.get("data") or {}
    status = data.get("status", 400 if error["code"] == -32602 else 404 if error["code"] == -32601 else 500)
    return status, json.dumps({"error": error["message"], "code": data.get("code"), "rpc_code": error["code"]}).encode()
