from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..tools.contracts import (
    RuntimeTimeoutAwareTool,
    RuntimeToolTimeoutError,
    ToolInvocation,
    ToolResult,
)
from ..tools.runtime_context import (
    RuntimeArtifactReadFacade,
    RuntimeLspToolFacade,
    RuntimeToolCatalogFacade,
    RuntimeTranscriptFacade,
    bind_runtime_tool_context,
)

_PROGRESS_QUEUE_MAX_ITEMS = 128
_PROGRESS_POLL_SECONDS = 0.05


@dataclass(slots=True)
class _ProgressDropTracker:
    """Thread-safe accounting for progress dropped by the bounded queue."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    count: int = 0
    first_ordinal: int | None = None
    last_ordinal: int | None = None
    streams: set[str] = field(default_factory=set)

    def _record_locked(self, *, ordinal: int, stream: object) -> None:
        self.count += 1
        if self.first_ordinal is None:
            self.first_ordinal = ordinal
        self.last_ordinal = ordinal
        if isinstance(stream, str) and stream:
            self.streams.add(stream)

    def _metadata_locked(self) -> dict[str, object] | None:
        if self.count == 0:
            return None
        return {
            "gap": True,
            "loss_reason": "progress_queue_full",
            "dropped_count": self.count,
            "dropped_ordinal_start": self.first_ordinal,
            "dropped_ordinal_end": self.last_ordinal,
            "dropped_streams": sorted(self.streams),
        }

    def take(self) -> dict[str, object] | None:
        with self.lock:
            metadata = self._metadata_locked()
            self.count = 0
            self.first_ordinal = None
            self.last_ordinal = None
            self.streams.clear()
            return metadata


@dataclass(frozen=True, slots=True)
class ToolExecutionProgress:
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ToolResultItem:
    result: ToolResult
    loss_metadata: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _ToolExceptionItem:
    exception: Exception
    loss_metadata: dict[str, object] | None = None


type _ToolQueueItem = ToolExecutionProgress | _ToolResultItem | _ToolExceptionItem


@dataclass(frozen=True, slots=True)
class RuntimeToolExecutor:
    workspace: Path
    lsp: RuntimeLspToolFacade
    lsp_diagnostics_on_write: bool = False
    tool_catalog: RuntimeToolCatalogFacade | None = None
    artifact: RuntimeArtifactReadFacade | None = None
    transcript: RuntimeTranscriptFacade | None = None

    def invoke(
        self,
        *,
        tool: Any,
        invocation: ToolInvocation,
    ) -> Generator[ToolExecutionProgress, None, ToolResult | Exception]:
        """Execute one runtime-owned invocation."""
        effective_timeout = invocation.context.tool_timeout_seconds
        if invocation.tool_call.tool_name == "shell_exec" or (effective_timeout is not None and not isinstance(tool, RuntimeTimeoutAwareTool)):
            return (
                yield from self._invoke_with_progress(
                    tool=tool,
                    invocation=invocation,
                    tool_timeout=effective_timeout,
                )
            )

        try:
            return self._invoke_tool(tool=tool, invocation=invocation, tool_timeout=effective_timeout)
        except Exception as exc:
            return exc

    def _invoke_tool(
        self,
        *,
        tool: Any,
        invocation: ToolInvocation,
        tool_timeout: int | None,
        emit_tool_progress: Callable[[Mapping[str, object]], None] | None = None,
    ) -> ToolResult:
        context = replace(
            invocation.context,
            emit_tool_progress=emit_tool_progress,
            lsp=self.lsp,
            lsp_diagnostics_on_write=self.lsp_diagnostics_on_write,
            tool_catalog=self.tool_catalog,
            artifact=self.artifact,
            transcript=self.transcript,
        )
        with bind_runtime_tool_context(context):
            if tool_timeout is not None and isinstance(tool, RuntimeTimeoutAwareTool):
                return tool.invoke_with_runtime_timeout(
                    invocation.tool_call,
                    workspace=self.workspace,
                    timeout_seconds=tool_timeout,
                )
            return tool.invoke(invocation.tool_call, workspace=self.workspace)

    def _invoke_with_progress(
        self,
        *,
        tool: Any,
        invocation: ToolInvocation,
        tool_timeout: int | None,
    ) -> Generator[ToolExecutionProgress, None, ToolResult | Exception]:
        tool_call = invocation.tool_call
        progress_queue: queue.Queue[_ToolQueueItem] = queue.Queue(maxsize=_PROGRESS_QUEUE_MAX_ITEMS)
        dropped = _ProgressDropTracker()
        invocation_id = invocation.context.invocation_id or tool_call.tool_call_id
        run_id = invocation.context.run_id
        next_fallback_ordinal = 1

        def emit_tool_progress(payload: Mapping[str, object]) -> None:
            nonlocal next_fallback_ordinal
            progress_payload: dict[str, object] = {
                "tool": tool_call.tool_name,
                **dict(payload),
            }
            progress_payload["tool"] = tool_call.tool_name
            if run_id is not None:
                progress_payload["run_id"] = run_id
            if invocation_id is not None:
                progress_payload["invocation_id"] = invocation_id
                progress_payload["tool_call_id"] = invocation_id

            with dropped.lock:
                raw_ordinal = progress_payload.get("ordinal")
                if isinstance(raw_ordinal, int) and not isinstance(raw_ordinal, bool):
                    ordinal = max(raw_ordinal, next_fallback_ordinal)
                else:
                    ordinal = next_fallback_ordinal
                progress_payload["ordinal"] = ordinal
                next_fallback_ordinal = ordinal + 1
                loss_metadata = dropped._metadata_locked()
                if loss_metadata is not None:
                    progress_payload.update(loss_metadata)
                try:
                    progress_queue.put_nowait(ToolExecutionProgress(progress_payload))
                except queue.Full:
                    dropped._record_locked(ordinal=ordinal, stream=progress_payload.get("stream"))
                else:
                    if loss_metadata is not None:
                        # The loss marker is carried by this first retained event.
                        dropped.count = 0
                        dropped.first_ordinal = None
                        dropped.last_ordinal = None
                        dropped.streams.clear()

        def invoke_tool() -> None:
            try:
                result = self._invoke_tool(
                    tool=tool,
                    invocation=invocation,
                    tool_timeout=tool_timeout,
                    emit_tool_progress=emit_tool_progress,
                )
                progress_queue.put(_ToolResultItem(result, dropped.take()))
            except Exception as exc:
                progress_queue.put(_ToolExceptionItem(exc, dropped.take()))

        worker = threading.Thread(
            target=invoke_tool,
            name=f"runtime-tool-{tool_call.tool_name}-worker",
            daemon=True,
        )
        worker.start()

        terminal_item: _ToolResultItem | _ToolExceptionItem | None = None
        deadline = time.monotonic() + tool_timeout if tool_timeout is not None and not isinstance(tool, RuntimeTimeoutAwareTool) else None
        while terminal_item is None:
            try:
                poll_timeout = _PROGRESS_POLL_SECONDS
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        terminal_item = _ToolExceptionItem(
                            RuntimeToolTimeoutError(f"tool '{tool_call.tool_name}' exceeded runtime timeout of {tool_timeout}s")
                        )
                        break
                    poll_timeout = min(poll_timeout, remaining)
                item = progress_queue.get(timeout=poll_timeout)
            except queue.Empty:
                if invocation.context.abort_signal is not None and invocation.context.abort_signal.cancelled:
                    reason = getattr(invocation.context.abort_signal, "reason", None)
                    terminal_item = _ToolExceptionItem(RuntimeError(reason if isinstance(reason, str) else "run interrupted"))
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    terminal_item = _ToolExceptionItem(
                        RuntimeToolTimeoutError(f"tool '{tool_call.tool_name}' exceeded runtime timeout of {tool_timeout}s")
                    )
                    break
                continue
            if isinstance(item, ToolExecutionProgress):
                yield item
                continue
            terminal_item = item

        while True:
            try:
                item = progress_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, ToolExecutionProgress):
                yield item

        if not (isinstance(terminal_item, _ToolExceptionItem) and isinstance(terminal_item.exception, RuntimeToolTimeoutError)):
            worker.join(timeout=1)
        if terminal_item is None:
            terminal_item = _ToolExceptionItem(RuntimeError("tool execution ended without a terminal result"))
        loss_metadata = terminal_item.loss_metadata
        if loss_metadata is None:
            loss_metadata = dropped.take()
        if loss_metadata is not None:
            loss_payload: dict[str, object] = {
                "tool": tool_call.tool_name,
                "stream": None,
                "chunk": "",
                "chunk_char_count": 0,
                "byte_count": 0,
                "ordinal": loss_metadata.get("dropped_ordinal_start"),
                **loss_metadata,
            }
            if run_id is not None:
                loss_payload["run_id"] = run_id
            if invocation_id is not None:
                loss_payload["invocation_id"] = invocation_id
                loss_payload["tool_call_id"] = invocation_id
            yield ToolExecutionProgress(loss_payload)
        if isinstance(terminal_item, _ToolExceptionItem):
            return terminal_item.exception
        return terminal_item.result
