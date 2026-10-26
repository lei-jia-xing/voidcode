from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast


class OpenAILoopbackServer(ThreadingHTTPServer):
    def __init__(self, replies: list[dict[str, object]]) -> None:
        super().__init__(("127.0.0.1", 0), _OpenAILoopbackHandler)
        self.replies = replies
        self.requests: list[dict[str, object]] = []
        self.request_headers: list[dict[str, str]] = []
        self.response_bodies: list[bytes] = []
        self.response_content_types: list[str] = []


class _OpenAILoopbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(OpenAILoopbackServer, self.server)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.request_headers.append({name: value for name, value in self.headers.items()})
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
    head: dict[str, object] = {"role": "assistant"}
    for key in ("content", "reasoning_content", "reasoning"):
        if key in message and message[key] is not None:
            head[key] = message[key]
    raw_calls = message.get("tool_calls")
    calls = [cast(dict[str, object], call) for call in raw_calls] if isinstance(raw_calls, list) else []
    # Faithful wire shape: one indexed tool-call entry per delta chunk, never
    # the whole message list in one delta (the wire never sends that).
    call_deltas = [{"tool_calls": [{**call, "index": index}]} for index, call in enumerate(calls)]
    chunks = [
        {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": head, "finish_reason": None}],
        },
        *(
            {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }
            for delta in call_deltas
        ),
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
    ]
    frames = (f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode() for chunk in chunks)
    return b"".join((*frames, b"data: [DONE]\n\n"))


_DEFAULT_USAGE: dict[str, object] = {"prompt_tokens": 120, "completion_tokens": 45, "prompt_tokens_details": {"cached_tokens": 30}}


def openai_response(
    *,
    message: dict[str, object],
    finish: str,
    usage: dict[str, object] | None | bool = True,
) -> dict[str, object]:
    """Build a faithful OpenAI chat-completion reply, usage block included by default.

    Pass ``usage=None`` explicitly only when the test's subject IS unknown-usage
    refusal; every other consumer gets the realistic wire behavior. ``usage=False``
    omits the block for legacy shape tests that assert its absence.
    """
    response_message = dict(message)
    if response_message.get("tool_calls") and not response_message.get("reasoning_content"):
        response_message["reasoning_content"] = "loopback tool-call reasoning"
    reply: dict[str, object] = {
        "id": "chatcmpl-loopback",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "message": response_message, "finish_reason": finish}],
    }
    if usage is True:
        reply["usage"] = dict(_DEFAULT_USAGE)
    elif usage is not None and usage is not False:
        reply["usage"] = usage
    return reply
