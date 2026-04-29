from __future__ import annotations

import contextlib
import json
import sys
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO, cast
from uuid import uuid4

from .. import __version__
from ..runtime.contracts import (
    RuntimeRequest,
    RuntimeStreamChunk,
    validate_runtime_request_metadata,
)

JSON_RPC_VERSION = "2.0"
MAX_JSON_RPC_LINE_BYTES = 1_048_576
MAX_PROMPT_CHARS = 65_536
MAX_SESSIONS = 128

_ERROR_PARSE = -32700
_ERROR_INVALID_REQUEST = -32600
_ERROR_METHOD_NOT_FOUND = -32601
_ERROR_INVALID_PARAMS = -32602
_ERROR_INTERNAL = -32603


type JsonObject = dict[str, object]
type JsonRpcId = str | int | None


class AcpRuntime(Protocol):
    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]: ...

    def cancel_session(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        reason: str | None = None,
    ) -> object: ...


@dataclass(slots=True)
class AcpSessionBinding:
    acp_session_id: str
    runtime_session_id: str | None = None
    active: bool = False
    cancel_requested: bool = False
    tool_call_ids_by_tool: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class StdioAcpServer:
    runtime: AcpRuntime
    workspace: Path
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    _sessions: dict[str, AcpSessionBinding] = field(default_factory=dict, init=False)
    _prompt_threads: list[threading.Thread] = field(default_factory=list, init=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _runtime_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _runtime_prompt_active: bool = field(default=False, init=False)

    def serve(self) -> int:
        for line in self.stdin:
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_JSON_RPC_LINE_BYTES:
                self._write_error(
                    None,
                    _ERROR_INVALID_REQUEST,
                    "request line is too large",
                    respond_without_id=True,
                )
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._write_error(
                    None,
                    _ERROR_PARSE,
                    f"parse error: {exc.msg}",
                    respond_without_id=True,
                )
                continue
            self._handle_loaded_request(request)
        self._join_all_prompt_threads()
        return 0

    def _handle_loaded_request(self, request: object) -> None:
        if not isinstance(request, dict):
            self._write_error(
                None,
                _ERROR_INVALID_REQUEST,
                "request must be an object",
                respond_without_id=True,
            )
            return
        payload = cast(dict[object, object], request)
        request_id, invalid_request_id = _request_id(payload.get("id"))
        if invalid_request_id:
            self._write_error(
                None,
                _ERROR_INVALID_REQUEST,
                "id must be a string, integer, null, or omitted",
                respond_without_id=True,
            )
            return
        if payload.get("jsonrpc") != JSON_RPC_VERSION:
            self._write_error(request_id, _ERROR_INVALID_REQUEST, "jsonrpc must be '2.0'")
            return
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            self._write_error(
                request_id, _ERROR_INVALID_REQUEST, "method must be a non-empty string"
            )
            return
        params = payload.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            self._write_error(request_id, _ERROR_INVALID_PARAMS, "params must be an object")
            return
        typed_params = cast(dict[str, object], params)

        try:
            if method == "initialize":
                self._write_result(request_id, self._initialize_result())
            elif method == "session/new":
                self._write_result(request_id, self._new_session_result(typed_params))
            elif method == "session/prompt":
                self._start_session_prompt(request_id, typed_params)
            elif method == "session/cancel":
                result = self._cancel_result(typed_params)
                if request_id is not None:
                    self._write_result(request_id, result)
            else:
                self._write_error(request_id, _ERROR_METHOD_NOT_FOUND, f"unknown method: {method}")
        except ValueError as exc:
            if request_id is not None or method != "session/cancel":
                self._write_error(request_id, _ERROR_INVALID_PARAMS, str(exc))
            else:
                self.stderr.write(f"ACP session/cancel ignored: {exc}\n")
                self.stderr.flush()
        except Exception as exc:
            self._log_exception("ACP request failed", exc)
            self._write_error(request_id, _ERROR_INTERNAL, "internal error")
        self._join_finished_prompt_threads()

    def _join_all_prompt_threads(self) -> None:
        for thread in tuple(self._prompt_threads):
            thread.join()
        self._prompt_threads.clear()

    def _join_finished_prompt_threads(self) -> None:
        alive_threads: list[threading.Thread] = []
        for thread in self._prompt_threads:
            if thread.is_alive():
                alive_threads.append(thread)
            else:
                thread.join()
        self._prompt_threads = alive_threads

    def _initialize_result(self) -> JsonObject:
        return {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {},
                "mcpCapabilities": {},
            },
            "agentInfo": {
                "name": "voidcode",
                "title": "VoidCode",
                "version": __version__,
            },
            "authMethods": [],
        }

    def _start_session_prompt(self, request_id: JsonRpcId, params: Mapping[str, object]) -> None:
        acp_session_id = _required_string(params, "sessionId")
        prompt = _prompt_text(params.get("prompt"))
        binding = self._sessions.get(acp_session_id)
        if binding is None:
            raise ValueError(f"unknown ACP session id: {acp_session_id}")
        if binding.active:
            raise ValueError(f"ACP session is already running a prompt: {acp_session_id}")
        with self._runtime_lock:
            if self._runtime_prompt_active:
                raise ValueError("another ACP prompt is already running")
            self._runtime_prompt_active = True
        binding.active = True
        binding.cancel_requested = False
        thread = threading.Thread(
            target=self._handle_session_prompt,
            args=(request_id, acp_session_id, prompt, binding),
            name=f"voidcode-acp-prompt-{acp_session_id}",
            daemon=False,
        )
        self._prompt_threads.append(thread)
        thread.start()

    def _new_session_result(self, params: Mapping[str, object] | None = None) -> JsonObject:
        if len(self._sessions) >= MAX_SESSIONS:
            raise ValueError("maximum ACP session count reached")
        if params is not None:
            raw_mcp_servers = params.get("mcpServers", [])
            if not isinstance(raw_mcp_servers, list):
                raise ValueError("params.mcpServers must be a list when provided")
            if raw_mcp_servers:
                raise ValueError("ACP MCP servers are not supported by the minimal stdio facade")
        acp_session_id = f"acp-session-{uuid4().hex}"
        self._sessions[acp_session_id] = AcpSessionBinding(acp_session_id=acp_session_id)
        return {"sessionId": acp_session_id}

    def _handle_session_prompt(
        self,
        request_id: JsonRpcId,
        acp_session_id: str,
        prompt: str,
        binding: AcpSessionBinding,
    ) -> None:
        stop_reason = "end_turn"
        failed_execution = False
        final_session_status: object = None
        try:
            request = RuntimeRequest(
                prompt=prompt,
                session_id=binding.runtime_session_id,
                metadata=validate_runtime_request_metadata({"agent": {"preset": "leader"}}),
                allocate_session_id=binding.runtime_session_id is None,
            )
            chunks = self.runtime.run_stream(request)
            while True:
                if binding.cancel_requested:
                    stop_reason = "cancelled"
                    break
                try:
                    with contextlib.redirect_stdout(self.stderr):
                        chunk = next(chunks)
                except StopIteration:
                    break
                if binding.cancel_requested:
                    stop_reason = "cancelled"
                    break
                if binding.runtime_session_id is None:
                    binding.runtime_session_id = chunk.session.session.id
                final_session_status = getattr(chunk.session, "status", None)
                if chunk.event is not None:
                    if _optional_attr(chunk.event, "event_type") == "runtime.failed":
                        failed_execution = True
                    self._write_runtime_event_update(
                        acp_session_id=acp_session_id,
                        runtime_session_id=chunk.session.session.id,
                        binding=binding,
                        event=chunk.event,
                    )
                if chunk.kind == "output":
                    self._write_session_update(
                        acp_session_id,
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": chunk.output or ""},
                        },
                    )
            if binding.runtime_session_id is None:
                raise RuntimeError("runtime stream emitted no chunks")
            if binding.cancel_requested:
                stop_reason = "cancelled"
            if failed_execution or final_session_status == "failed":
                self._write_error(request_id, _ERROR_INTERNAL, "runtime execution failed")
                return
            self._write_result(
                request_id,
                {"stopReason": stop_reason},
            )
        except Exception as exc:
            self._log_exception("ACP prompt failed", exc)
            self._write_session_update(
                acp_session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Runtime failed."},
                },
            )
            self._write_error(request_id, _ERROR_INTERNAL, "runtime execution failed")
        finally:
            binding.active = False
            with self._runtime_lock:
                self._runtime_prompt_active = False

    def _cancel_result(self, params: Mapping[str, object]) -> JsonObject:
        acp_session_id = _required_string(params, "sessionId")
        binding = self._sessions.get(acp_session_id)
        if binding is None:
            raise ValueError(f"unknown ACP session id: {acp_session_id}")
        binding.cancel_requested = True
        cancel_payload: dict[str, object] | None = None
        if binding.runtime_session_id is not None:
            cancel_session = getattr(self.runtime, "cancel_session", None)
            if callable(cancel_session):
                result = cancel_session(
                    binding.runtime_session_id,
                    reason="acp session/cancel",
                )
                as_payload = getattr(result, "as_payload", None)
                if callable(as_payload):
                    cancel_payload = cast(dict[str, object], as_payload())
                elif isinstance(result, dict):
                    cancel_payload = cast(dict[str, object], result)
        interrupted = (
            bool(cancel_payload.get("interrupted")) if cancel_payload is not None else False
        )
        return {
            "sessionId": acp_session_id,
            "runtimeSessionId": binding.runtime_session_id,
            "cancelled": interrupted,
            "stopReason": "cancelled" if interrupted or binding.active else "not_active",
            "supported": cancel_payload is not None,
            "runtimeCancel": cancel_payload,
        }

    def _write_runtime_event_update(
        self,
        *,
        acp_session_id: str,
        runtime_session_id: str,
        binding: AcpSessionBinding,
        event: object,
    ) -> None:
        event_type = _optional_attr(event, "event_type")
        payload = _mapping_attr(event, "payload")
        if event_type == "graph.tool_request_created":
            tool_name = _string_payload(payload, "tool", default="tool")
            tool_call_id = f"{runtime_session_id}:{tool_name}:{_optional_attr(event, 'sequence')}"
            binding.tool_call_ids_by_tool[tool_name] = tool_call_id
            self._write_session_update(
                acp_session_id,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_call_id,
                    "title": tool_name,
                    "kind": "other",
                    "status": "pending",
                },
            )
            return
        if event_type == "runtime.tool_started":
            return
        if event_type == "runtime.tool_completed":
            tool_name = _string_payload(payload, "tool", default="tool")
            self._write_session_update(
                acp_session_id,
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": binding.tool_call_ids_by_tool.get(
                        tool_name, _tool_call_id(payload, runtime_session_id)
                    ),
                    "status": "completed",
                },
            )
            return
        if event_type == "runtime.failed":
            self._write_session_update(
                acp_session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {
                        "type": "text",
                        "text": "Runtime failed.",
                    },
                },
            )
            return
        self._write_session_update(
            acp_session_id,
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {
                    "type": "text",
                    "text": json.dumps(_event_payload(event), separators=(",", ":")),
                },
            },
        )

    def _write_session_update(self, session_id: str, update: JsonObject) -> None:
        self._write_notification(
            "session/update",
            {
                "sessionId": session_id,
                "update": update,
            },
        )

    def _write_result(self, request_id: JsonRpcId, result: JsonObject) -> None:
        if request_id is None:
            return
        self._write_json({"jsonrpc": JSON_RPC_VERSION, "id": request_id, "result": result})

    def _write_error(
        self,
        request_id: JsonRpcId,
        code: int,
        message: str,
        *,
        respond_without_id: bool = False,
    ) -> None:
        if request_id is None and not respond_without_id:
            return
        self._write_json(
            {
                "jsonrpc": JSON_RPC_VERSION,
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _write_notification(self, method: str, params: JsonObject) -> None:
        self._write_json({"jsonrpc": JSON_RPC_VERSION, "method": method, "params": params})

    def _write_json(self, payload: JsonObject) -> None:
        with self._write_lock:
            self.stdout.write(json.dumps(payload, separators=(",", ":")))
            self.stdout.write("\n")
            self.stdout.flush()

    def _log_exception(self, message: str, exc: Exception) -> None:
        self.stderr.write(f"{message}: {exc}\n")
        self.stderr.flush()


def _request_id(value: object) -> tuple[JsonRpcId, bool]:
    if value is None or isinstance(value, str):
        return value, False
    if isinstance(value, int) and not isinstance(value, bool):
        return value, False
    return None, True


def _required_string(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"params.{name} must be a non-empty string")
    return value


def _prompt_text(value: object) -> str:
    if isinstance(value, str) and value:
        return _validate_prompt_length(value)
    if isinstance(value, list):
        parts: list[str] = []
        for index, item in enumerate(cast(list[object], value)):
            if not isinstance(item, dict):
                raise ValueError(f"params.prompt[{index}] must be an object")
            block = cast(dict[object, object], item)
            if block.get("type") != "text":
                raise ValueError("only text prompt blocks are supported")
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError(f"params.prompt[{index}].text must be a string")
            parts.append(text)
        prompt = "\n".join(parts).strip()
        if prompt:
            return _validate_prompt_length(prompt)
    raise ValueError("params.prompt must be a non-empty string or text block array")


def _validate_prompt_length(prompt: str) -> str:
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError("params.prompt is too large")
    return prompt


def _event_payload(event: object) -> JsonObject:
    return {
        "sessionId": _optional_attr(event, "session_id"),
        "sequence": _optional_attr(event, "sequence"),
        "type": _optional_attr(event, "event_type"),
        "source": _optional_attr(event, "source"),
        "payload": _mapping_attr(event, "payload"),
    }


def _optional_attr(value: object, name: str) -> object:
    return getattr(value, name, None)


def _mapping_attr(value: object, name: str) -> JsonObject:
    raw = getattr(value, name, {})
    if isinstance(raw, dict):
        return cast(JsonObject, raw)
    return {}


def _string_payload(payload: Mapping[str, object], key: str, *, default: str = "") -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value else default


def _tool_call_id(payload: Mapping[str, object], runtime_session_id: str) -> str:
    for key in ("tool_call_id", "call_id", "request_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    tool_name = _string_payload(payload, "tool", default="tool")
    return f"{runtime_session_id}:{tool_name}"
