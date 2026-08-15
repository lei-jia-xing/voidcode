from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..provider.protocol import ProviderAbortSignal
from ..tools.contracts import (
    RuntimeTimeoutAwareTool,
    RuntimeToolTimeoutError,
    ToolCall,
    ToolResult,
)
from ..tools.runtime_context import (
    RuntimeArtifactReadFacade,
    RuntimeLspToolFacade,
    RuntimeMemoryToolFacade,
    RuntimeToolCatalogFacade,
    RuntimeToolInvocationContext,
    bind_runtime_tool_context,
)

_PROGRESS_QUEUE_MAX_ITEMS = 128
_PROGRESS_POLL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class ToolExecutionProgress:
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ToolResultItem:
    result: ToolResult


@dataclass(frozen=True, slots=True)
class _ToolExceptionItem:
    exception: Exception


type _ToolQueueItem = ToolExecutionProgress | _ToolResultItem | _ToolExceptionItem


@dataclass(frozen=True, slots=True)
class RuntimeToolExecutor:
    workspace: Path
    memory: RuntimeMemoryToolFacade
    lsp: RuntimeLspToolFacade
    lsp_diagnostics_on_write: bool = False
    tool_catalog: RuntimeToolCatalogFacade | None = None
    artifact: RuntimeArtifactReadFacade | None = None

    def invoke(
        self,
        *,
        tool: Any,
        tool_call: ToolCall,
        read_paths: frozenset[str],
        read_lines: Mapping[str, frozenset[int]],
        tool_timeout: int | None,
        session_id: str,
        parent_session_id: str | None,
        delegation_depth: int,
        remaining_spawn_budget: int | None,
        abort_signal: ProviderAbortSignal | None,
        model: str | None = None,
    ) -> Generator[ToolExecutionProgress, None, ToolResult | Exception]:
        if tool_call.tool_name == "shell_exec" or (tool_timeout is not None and not isinstance(tool, RuntimeTimeoutAwareTool)):
            return (
                yield from self._invoke_with_progress(
                    tool=tool,
                    tool_call=tool_call,
                    read_paths=read_paths,
                    read_lines=read_lines,
                    tool_timeout=tool_timeout,
                    session_id=session_id,
                    parent_session_id=parent_session_id,
                    delegation_depth=delegation_depth,
                    remaining_spawn_budget=remaining_spawn_budget,
                    abort_signal=abort_signal,
                    model=model,
                )
            )

        try:
            return self._invoke_tool(
                tool=tool,
                tool_call=tool_call,
                read_paths=read_paths,
                read_lines=read_lines,
                tool_timeout=tool_timeout,
                session_id=session_id,
                parent_session_id=parent_session_id,
                delegation_depth=delegation_depth,
                remaining_spawn_budget=remaining_spawn_budget,
                abort_signal=abort_signal,
                model=model,
            )
        except Exception as exc:
            return exc

    def _invoke_tool(
        self,
        *,
        tool: Any,
        tool_call: ToolCall,
        read_paths: frozenset[str],
        read_lines: Mapping[str, frozenset[int]],
        tool_timeout: int | None,
        session_id: str,
        parent_session_id: str | None,
        delegation_depth: int,
        remaining_spawn_budget: int | None,
        abort_signal: ProviderAbortSignal | None,
        model: str | None = None,
        emit_tool_progress: Callable[[Mapping[str, object]], None] | None = None,
    ) -> ToolResult:
        with bind_runtime_tool_context(
            RuntimeToolInvocationContext(
                session_id=session_id,
                parent_session_id=parent_session_id,
                delegation_depth=delegation_depth,
                remaining_spawn_budget=remaining_spawn_budget,
                read_paths=read_paths,
                read_lines=read_lines,
                model=model,
                abort_signal=abort_signal,
                emit_tool_progress=emit_tool_progress,
                memory=self.memory,
                lsp=self.lsp,
                lsp_diagnostics_on_write=self.lsp_diagnostics_on_write,
                tool_catalog=self.tool_catalog,
                artifact=self.artifact,
            )
        ):
            if tool_timeout is not None and isinstance(tool, RuntimeTimeoutAwareTool):
                return tool.invoke_with_runtime_timeout(
                    tool_call,
                    workspace=self.workspace,
                    timeout_seconds=tool_timeout,
                )
            return tool.invoke(tool_call, workspace=self.workspace)

    def _invoke_with_progress(
        self,
        *,
        tool: Any,
        tool_call: ToolCall,
        read_paths: frozenset[str],
        read_lines: Mapping[str, frozenset[int]],
        tool_timeout: int | None,
        session_id: str,
        parent_session_id: str | None,
        delegation_depth: int,
        remaining_spawn_budget: int | None,
        abort_signal: ProviderAbortSignal | None,
        model: str | None = None,
    ) -> Generator[ToolExecutionProgress, None, ToolResult | Exception]:
        progress_queue: queue.Queue[_ToolQueueItem] = queue.Queue(maxsize=_PROGRESS_QUEUE_MAX_ITEMS)

        def emit_tool_progress(payload: Mapping[str, object]) -> None:
            progress_payload: dict[str, object] = {
                "tool": tool_call.tool_name,
                **dict(payload),
            }
            try:
                progress_queue.put_nowait(ToolExecutionProgress(progress_payload))
            except queue.Full:
                pass

        def invoke_tool() -> None:
            try:
                result = self._invoke_tool(
                    tool=tool,
                    tool_call=tool_call,
                    read_paths=read_paths,
                    read_lines=read_lines,
                    tool_timeout=tool_timeout,
                    session_id=session_id,
                    parent_session_id=parent_session_id,
                    delegation_depth=delegation_depth,
                    remaining_spawn_budget=remaining_spawn_budget,
                    abort_signal=abort_signal,
                    model=model,
                    emit_tool_progress=emit_tool_progress,
                )
                progress_queue.put(_ToolResultItem(result))
            except Exception as exc:
                progress_queue.put(_ToolExceptionItem(exc))

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
                if abort_signal is not None and abort_signal.cancelled:
                    reason = getattr(abort_signal, "reason", None)
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
        if isinstance(terminal_item, _ToolExceptionItem):
            return terminal_item.exception
        return terminal_item.result
