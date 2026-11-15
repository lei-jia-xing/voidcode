"""External fixture provider for the in-flight resource-read cancellation regression."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from voidcode_test_resource_read_cancel_gate import read_entered, read_page_ready, release_read

from voidcode.capabilities.exec.process_reader import read_process_resource
from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.resources import (
    ResourcePage,
    ResourceReadCaller,
    ResourceReader,
    ResourceReaderServices,
    ResourceReadRequest,
)
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess

_LSP_FIXTURE = Path(__file__).resolve().parents[1] / "lsp_echo_server.py"


def _frame(message: Mapping[str, object]) -> bytes:
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body


def _has_response(data: bytes, identity: int) -> bool:
    remaining = data
    marker = b"\r\n\r\n"
    while True:
        header_end = remaining.find(marker)
        if header_end < 0:
            return False
        header = remaining[:header_end]
        lengths = [line.split(b":", 1)[1].strip() for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:")]
        if not lengths:
            return False
        body_start = header_end + len(marker)
        body_end = body_start + int(lengths[0])
        if len(remaining) < body_end:
            return False
        try:
            response = json.loads(remaining[body_start:body_end].decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return False
        if response.get("id") == identity:
            return True
        remaining = remaining[body_end:]


def _find_handle(services: object, process_id: str) -> object | None:
    return next((handle for handle in services.list_processes() if handle.process_id == process_id), None)


def _wait_response(services: object, process_id: str, identity: int) -> bool:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        page = services.read_process_output(process_id, stream="stdout", offset=0, limit=65536)
        if _has_response(page.data, identity):
            return True
        handle = _find_handle(services, process_id)
        if handle is None or not handle.running:
            return False
        time.sleep(0.01)
    return False


class StartService:
    def __init__(self) -> None:
        self.definition = ToolDefinition(
            name="resource_service_start",
            description="Start the managed LSP service used by the resource-read cancellation fixture.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effects=frozenset({ToolEffect.SPAWN, ToolEffect.WRITE}),
        )

    def invoke(self, call, *, context, services):
        record = context.workspace / "resource-reader-lsp.jsonl"
        handle = services.spawn_service_process([sys.executable, "-u", str(_LSP_FIXTURE), "--record", str(record)])
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"rootUri": "file:///", "capabilities": {}},
        }
        services.write_process(handle.process_id, _frame(request))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            page = services.read_process_output(handle.process_id, stream="stdout", offset=0, limit=65536)
            if _has_response(page.data, 1):
                return ToolSuccess(
                    call.tool_name,
                    TextOutput(json.dumps({"process_id": handle.process_id, "pid": handle.pid})),
                )
            if not page.running:
                break
            time.sleep(0.01)
        return ToolFailure(call.tool_name, "managed LSP fixture did not answer initialize")


class FinishService:
    definition = CommandDefinition(
        name="resource_service_finish",
        description="Prove the managed service can still answer and exit after a resource read is cancelled.",
        argument_schema={
            "type": "object",
            "properties": {"process_id": {"type": "string"}},
            "required": ["process_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.WRITE}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services) -> CommandResult:
        process_id = str(invocation.arguments["process_id"])
        try:
            services.write_process(
                process_id,
                _frame(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "textDocument/hover",
                        "params": {
                            "textDocument": {"uri": "file:///resource-cancel.txt"},
                            "position": {"line": 0, "character": 0},
                        },
                    }
                ),
            )
            if not _wait_response(services, process_id, 2):
                return CommandResult(status="error", error="managed LSP service did not answer hover after the cancelled read")
            services.write_process(process_id, _frame({"jsonrpc": "2.0", "id": 3, "method": "shutdown", "params": {}}))
            if not _wait_response(services, process_id, 3):
                return CommandResult(status="error", error="managed LSP service did not answer shutdown")
            services.write_process(process_id, _frame({"jsonrpc": "2.0", "method": "exit", "params": {}}))
        except Exception as exc:
            return CommandResult(status="error", error=f"managed LSP service continuation failed: {type(exc).__name__}: {exc}")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            handle = _find_handle(services, process_id)
            if handle is None:
                return CommandResult(status="error", error="managed LSP service disappeared before exit status was observed")
            if not handle.running:
                return CommandResult(
                    status="ok",
                    output="managed LSP service responded and exited",
                    data={"hover_response": True, "shutdown_response": True, "running": False, "exit_code": handle.exit_code},
                )
            time.sleep(0.01)
        return CommandResult(status="error", error="managed LSP service did not exit after shutdown/exit")


def _gated_process_read(
    request: ResourceReadRequest,
    caller: ResourceReadCaller,
    services: ResourceReaderServices,
) -> ResourcePage | None:
    if request.scheme != "safetyproc":
        return None
    if services.owner != "resource-read-fixture":
        raise AssertionError(f"unexpected reader owner {services.owner!r}")
    process_id = request.uri.partition("://")[2].partition("/")[0]
    if not process_id:
        return None
    read_entered.set()
    page = read_process_resource(replace(request, uri=f"proc://{process_id}"), caller, services)
    if page is None:
        return None
    read_page_ready.set()
    if not release_read.wait(timeout=30):
        raise TimeoutError("resource reader cancellation gate was not released")
    return page


def create_plugin(config: Mapping[str, object]) -> Plugin:
    del config
    return Plugin(
        tools=(StartService(),),
        commands=(FinishService(),),
        resource_readers=(ResourceReader("safetyproc", _gated_process_read),),
    )
