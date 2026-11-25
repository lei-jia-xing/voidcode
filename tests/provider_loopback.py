from __future__ import annotations

import json
import time
from collections.abc import Callable
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
        self.model_listing_status = 200
        #: HTTP status for chat-completions requests; 401 lets a test prove error redaction.
        self.chat_status = 200
        #: Optional test hook run after each request is recorded, before its reply
        #: is chosen. Returning a reply overrides the queued one for that request.
        self.before_reply: Callable[[dict[str, object], int], dict[str, object] | None] | None = None
        #: Seconds to sleep between SSE frames so a TUI test can observe live output.
        self.stream_delay = 0.0
        #: Split streamed assistant content into deltas of at most this many chars.
        self.stream_chunk_chars = 0


class _OpenAILoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = cast(OpenAILoopbackServer, self.server)
        request: dict[str, object] = {"method": "GET", "path": self.path}
        server.request_headers.append({name: value for name, value in self.headers.items()})
        server.requests.append(request)
        if self.path != "/v1/models":
            self.send_error(404)
            return
        override = server.before_reply(request, len(server.requests)) if server.before_reply is not None else None
        response = override if override is not None else server.replies.pop(0)
        encoded = json.dumps(response).encode()
        server.response_bodies.append(encoded)
        server.response_content_types.append("application/json")
        self.send_response(server.model_listing_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        server = cast(OpenAILoopbackServer, self.server)
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        server.request_headers.append({name: value for name, value in self.headers.items()})
        server.requests.append(payload)
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        override = server.before_reply(payload, len(server.requests)) if server.before_reply is not None else None
        response = override if override is not None else server.replies.pop(0)
        streaming = payload.get("stream") is True
        stream_options = payload.get("stream_options")
        include_usage = isinstance(stream_options, dict) and stream_options.get("include_usage") is True
        frames: list[bytes] = []
        if server.chat_status != 200 or "choices" not in response:
            encoded = json.dumps(response).encode()
            content_type = "application/json"
        else:
            if streaming:
                frames = _stream_response_frames(response, include_usage=include_usage, chunk_chars=server.stream_chunk_chars)
                encoded = b"".join(frames)
            else:
                encoded = json.dumps(response).encode()
            content_type = "text/event-stream" if streaming else "application/json"
        server.response_bodies.append(encoded)
        server.response_content_types.append(content_type)
        self.send_response(server.chat_status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if frames and server.stream_delay > 0:
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()
                time.sleep(server.stream_delay)
        else:
            self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args


def _stream_response_body(response: dict[str, object], *, include_usage: bool) -> bytes:
    return b"".join(_stream_response_frames(response, include_usage=include_usage))


def _stream_response_frames(response: dict[str, object], *, include_usage: bool, chunk_chars: int = 0) -> list[bytes]:
    choice = cast(dict[str, object], cast(list[object], response["choices"])[0])
    message = cast(dict[str, object], choice["message"])
    head: dict[str, object] = {"role": "assistant"}
    trailing: list[dict[str, object]] = []
    content = message.get("content")
    if chunk_chars > 0 and isinstance(content, str) and len(content) > chunk_chars:
        # Progressive content deltas let a live TUI render partial text and a
        # reader observe it before the turn settles.
        trailing = [{"content": content[start : start + chunk_chars]} for start in range(0, len(content), chunk_chars)]
        head["content"] = ""
    else:
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
            for delta in trailing
        ),
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
    # The real wire sends the usage block as a terminal usage-only chunk after
    # the finish chunk, and only when the request asked for it with
    # stream_options.include_usage. A reply without a usage block (usage=None)
    # sends no chunk, so unknown usage stays unknown; a reported zero stays a
    # zero because the block is forwarded verbatim.
    usage = response.get("usage")
    if include_usage and isinstance(usage, dict):
        chunks.append(
            {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": response["created"],
                "model": response["model"],
                "choices": [],
                "usage": usage,
            }
        )
    return [
        *(f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode() for chunk in chunks),
        b"data: [DONE]\n\n",
    ]


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
