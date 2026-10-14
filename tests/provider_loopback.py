from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast


class OpenAILoopbackServer(ThreadingHTTPServer):
    def __init__(self, replies: list[dict[str, object]]) -> None:
        super().__init__(("127.0.0.1", 0), _OpenAILoopbackHandler)
        self.replies = replies
        self.requests: list[dict[str, object]] = []
        self.response_bodies: list[bytes] = []
        self.response_content_types: list[str] = []


class _OpenAILoopbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(OpenAILoopbackServer, self.server)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.requests.append(payload)
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        response = server.replies.pop(0)
        streaming = payload.get("stream") is True
        encoded = _stream_response_body(response) if streaming else json.dumps(response).encode()
        content_type = "text/event-stream" if streaming else "application/json"
        server.response_bodies.append(encoded)
        server.response_content_types.append(content_type)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


def _stream_response_body(response: dict[str, object]) -> bytes:
    choice = cast(dict[str, object], cast(list[object], response["choices"])[0])
    message = cast(dict[str, object], choice["message"])
    delta: dict[str, object] = {"role": "assistant"}
    for key in ("content", "reasoning_content", "reasoning", "tool_calls"):
        if key in message:
            delta[key] = message[key]
    chunks = (
        {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": choice["finish_reason"],
                }
            ],
        },
    )
    frames = (f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode() for chunk in chunks)
    return b"".join((*frames, b"data: [DONE]\n\n"))


def openai_response(*, message: dict[str, object], finish: str) -> dict[str, object]:
    response_message = dict(message)
    if response_message.get("tool_calls") and not response_message.get("reasoning_content"):
        response_message["reasoning_content"] = "loopback tool-call reasoning"
    return {
        "id": "chatcmpl-loopback",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "message": response_message, "finish_reason": finish}],
    }


def configure_openai_provider(
    workspace: Path,
    base_url: str,
    *,
    provider: str = "deepseek",
    model: str = "deepseek-v4-pro",
) -> None:
    config = {
        "approval_mode": "ask",
        "model": f"{provider}/{model}",
        "providers": {provider: {"base_url": base_url, "timeout_seconds": 5}},
    }
    (workspace / ".voidcode.json").write_text(json.dumps(config), encoding="utf-8")
