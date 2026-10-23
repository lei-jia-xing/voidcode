"""Deterministic tiny JSON-RPC stdio language server for LSP consumer tests.

Reads LSP-framed JSON-RPC from stdin, answers `initialize` with the
`LSP_ENCODING` position encoding (default utf-16) and full-sync open/close,
echoes hover params as plaintext and other request params as JSON, and records each
received method plus didOpen/didChange versions into `--record` (JSON lines).
Spawns the `--grandchild` command (when given) so cancellation tests can watch
a real descendant pid die with the server. With `--hang-after N`, the server
stops answering (but stays alive) after N JSON-RPC responses, so a second LSP
invocation blocks mid-wait until the caller cancels or times out.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def _read_exact(stream, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def _read_message(stream):
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = stream.read(1)
        if not chunk:
            raise EOFError
        header += chunk
    length = int(header.decode("ascii").split("Content-Length:")[1].split("\r\n")[0].strip())
    return json.loads(_read_exact(stream, length).decode("utf-8"))


def _write_message(stream, message):
    body = json.dumps(message).encode("utf-8")
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", required=True)
    parser.add_argument("--grandchild", default=None)
    parser.add_argument("--hang-after", type=int, default=None)
    parser.add_argument("--initialize-result", default=None)
    parser.add_argument("--server-request-id", choices=("same", "different", "string"))
    args = parser.parse_args()
    encoding = os.environ.get("LSP_ENCODING", "utf-16")
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    record = open(args.record, "a", encoding="utf-8")
    if args.grandchild:
        subprocess.Popen(args.grandchild, shell=True)
        record.write(json.dumps({"method": "__grandchild__", "pid": os.getpid()}) + "\n")
        record.flush()
    next_id = 0
    responses_sent = 0
    try:
        while True:
            message = _read_message(stdin)
            method = message.get("method", "")
            record.write(json.dumps({"method": method, "id": message.get("id")}) + "\n")
            record.flush()
            if method in ("initialized", "exit", "workspace/didChangeConfiguration"):
                if method == "exit":
                    return 0
                continue
            if method in ("textDocument/didOpen", "textDocument/didChange"):
                doc = message["params"]["textDocument"]
                record.write(json.dumps({"method": method + ":version", "version": doc.get("version")}) + "\n")
                record.flush()
                continue
            # Hang only after recording a real request past the answered quota: the
            # request is genuinely in flight (and the client genuinely blocked waiting
            # for its response) while `__hanging__` marks the deterministic barrier.
            if args.hang_after is not None and responses_sent >= args.hang_after:
                record.write(json.dumps({"method": "__hanging__"}) + "\n")
                record.flush()
                while True:
                    time.sleep(3600)
            if method == "shutdown":
                response = {"jsonrpc": "2.0", "id": message.get("id"), "result": None}
            elif method == "initialize":
                if args.server_request_id:
                    server_id = message["id"] if args.server_request_id == "same" else 900 if args.server_request_id == "different" else "server-id"
                    _write_message(stdout, {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 3, "message": "notice"}})
                    _write_message(stdout, {"jsonrpc": "2.0", "id": server_id, "method": "test/unsupported", "params": {}})
                    reply = _read_message(stdin)
                    record.write(json.dumps({"method": "__client_reply__", "reply": reply}) + "\n")
                    record.flush()
                next_id = message.get("id", 0)
                response = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": (
                        json.loads(args.initialize_result)
                        if args.initialize_result is not None
                        else {"capabilities": {"positionEncoding": encoding, "textDocumentSync": {"openClose": True, "change": 1}}}
                    ),
                }
            elif method == "textDocument/hover":
                response = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {"contents": {"kind": "plaintext", "value": json.dumps(message.get("params"))}},
                }
            else:
                response = {"jsonrpc": "2.0", "id": message.get("id"), "result": {"echo": message.get("params")}}
            _write_message(stdout, response)
            responses_sent += 1
            _ = next_id
    except EOFError, BrokenPipeError:
        return 0
    finally:
        record.close()


if __name__ == "__main__":
    raise SystemExit(main())
