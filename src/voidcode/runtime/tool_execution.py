from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..core.tool_context import EditSchema, LspDiagnostics, LspRequester, McpRequester, RuleReader, ToolCatalog, ToolCommandHandler
from ..provider.protocol import ProviderAbortSignal
from ..skills.models import SkillMetadata
from ..tools.contracts import (
    RuntimeTimeoutAwareTool,
    RuntimeToolTimeoutError,
    Tool,
    ToolInvocation,
    ToolResult,
)
from .execution.tool_resources import (
    SessionArtifactReader,
    SessionTranscriptReader,
    bind_lsp_request,
    bind_mcp_request,
    bind_rule_reader,
    bind_tool_command,
)
from .execution_ownership import EXECUTION_OWNERSHIP

logger = logging.getLogger(__name__)

_PROGRESS_QUEUE_MAX_ITEMS = 128
_PROGRESS_POLL_SECONDS = 0.05
#: Bounded reap window after the runtime cancels a timed-out invocation. The
#: runtime stops waiting here; when the worker has not exited by then, the tool
#: result reports the execution as possibly still in flight.
_TOOL_TIMEOUT_REAP_SECONDS = 0.5


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


@dataclass(slots=True)
class _InvocationCancelSignal:
    """Cancellation view the runtime hands to one running tool invocation.

    It mirrors the run-scoped abort signal and adds the runtime's own timeout
    cancellation, so a tool that polls ``context.abort_signal.cancelled`` sees
    both causes through the same slot the interrupt path uses. Cancelling one
    timed-out invocation must not cancel the run.
    """

    run_signal: ProviderAbortSignal | None = None
    _cancelled: bool = False
    _reason: str | None = None

    @property
    def cancelled(self) -> bool:
        if self._cancelled:
            return True
        return self.run_signal is not None and self.run_signal.cancelled

    @property
    def reason(self) -> str | None:
        if self._cancelled:
            return self._reason
        return self.run_signal.reason if self.run_signal is not None else None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        if value:
            self._reason = reason


def _timeout_cancellation_reason(timeout_seconds: int) -> str:
    return f"runtime tool timeout after {timeout_seconds}s"


def _runtime_timeout_error(
    *,
    tool_name: str,
    timeout_seconds: int,
    cancellation_signalled: bool,
    execution_stopped: bool,
) -> RuntimeToolTimeoutError:
    return RuntimeToolTimeoutError(
        f"tool '{tool_name}' exceeded runtime timeout of {timeout_seconds}s",
        cancellation_signalled=cancellation_signalled,
        execution_stopped=execution_stopped,
    )


def _drain_terminal_item(progress_queue: queue.Queue[_ToolQueueItem]) -> _ToolResultItem | _ToolExceptionItem | None:
    """Discard queued progress and return the first terminal item, if any.

    ``invoke_tool`` enqueues exactly one terminal item per invocation (a result
    or an exception), so the first one observed is the whole outcome; anything
    that follows it can only be progress that was queued behind it.
    """
    terminal: _ToolResultItem | _ToolExceptionItem | None = None
    while True:
        try:
            item = progress_queue.get_nowait()
        except queue.Empty:
            return terminal
        if isinstance(item, ToolExecutionProgress) or terminal is not None:
            continue
        terminal = item


def _log_late_completion(
    *,
    tool_name: str,
    invocation_id: str | None,
    item: _ToolResultItem | _ToolExceptionItem,
) -> None:
    """Record a terminal item produced after the runtime stopped waiting."""
    outcome = f"status={item.result.status}" if isinstance(item, _ToolResultItem) else f"exception={type(item.exception).__name__}"
    logger.warning(
        "tool %s produced a terminal result after the runtime abandoned its timed-out execution; the late result was discarded (%s) tool_call_id=%s",
        tool_name,
        outcome,
        invocation_id,
    )


def _observe_abandoned_execution(
    *,
    worker: threading.Thread,
    progress_queue: queue.Queue[_ToolQueueItem],
    tool_name: str,
    invocation_id: str | None,
) -> None:
    """Drain and record an execution the runtime abandoned before it stopped.

    Draining keeps the bounded progress queue from blocking the abandoned
    worker, and the terminal item it eventually produces is logged for
    diagnosis instead of being committed as the tool result. Logging the first
    terminal item is the whole outcome: an invocation produces exactly one, so
    the observer stops there and exits when the worker does.
    """
    while True:
        if not worker.is_alive():
            late_item = _drain_terminal_item(progress_queue)
            if late_item is not None:
                _log_late_completion(tool_name=tool_name, invocation_id=invocation_id, item=late_item)
            return
        try:
            item = progress_queue.get(timeout=_PROGRESS_POLL_SECONDS)
        except queue.Empty:
            continue
        if isinstance(item, ToolExecutionProgress):
            continue
        _log_late_completion(tool_name=tool_name, invocation_id=invocation_id, item=item)
        return


@dataclass(frozen=True, slots=True)
class RuntimeToolExecutor:
    workspace: Path
    lsp: LspDiagnostics | None = None
    lsp_diagnostics_on_write: bool = False
    tool_catalog: ToolCatalog | None = None
    read_artifact: Callable[..., dict[str, object]] | None = None
    read_transcript: Callable[..., dict[str, object] | None] | None = None
    read_rule: RuleReader | None = None
    resolve_skill: Callable[[str], SkillMetadata] | None = None
    resolve_edit_schema: Callable[[str | None], EditSchema] | None = None
    lsp_request: LspRequester | None = None
    mcp_request: McpRequester | None = None
    task_command: ToolCommandHandler | None = None
    task_batch_command: ToolCommandHandler | None = None
    process_command: ToolCommandHandler | None = None

    def invoke(
        self,
        *,
        tool: Tool,
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
        tool: Tool,
        invocation: ToolInvocation,
        tool_timeout: int | None,
        emit_tool_progress: Callable[[Mapping[str, object]], None] | None = None,
        cancel_signal: _InvocationCancelSignal | None = None,
    ) -> ToolResult:
        tool_name = invocation.tool_call.tool_name
        diagnostics_enabled = self.lsp_diagnostics_on_write and tool_name in {"write", "edit", "multi_edit", "apply_patch", "apply_workspace_edit"}
        context = replace(
            invocation.context,
            workspace=self.workspace,
            emit_tool_progress=emit_tool_progress,
            abort_signal=cancel_signal if cancel_signal is not None else invocation.context.abort_signal,
            edit_schema=(
                self.resolve_edit_schema(invocation.context.model)
                if self.resolve_edit_schema is not None and tool_name in {"edit", "multi_edit"}
                else invocation.context.edit_schema
            ),
            lsp=self.lsp if diagnostics_enabled else None,
            lsp_diagnostics_on_write=diagnostics_enabled,
            tool_catalog=None,
            artifact=None,
            transcript=None,
            read_rule=None,
            resolve_skill=None,
            lsp_request=None,
            mcp_request=None,
            task_runtime=None,
            task_batch_runtime=None,
            process_runtime=None,
        )
        if tool_name == "read":
            caller_session_id = context.require_session_id()
            context = replace(
                context,
                tool_catalog=self.tool_catalog,
                artifact=SessionArtifactReader(caller_session_id, self.read_artifact) if self.read_artifact is not None else None,
                transcript=SessionTranscriptReader(caller_session_id, self.read_transcript) if self.read_transcript is not None else None,
                read_rule=bind_rule_reader(self.read_rule, workspace=self.workspace) if self.read_rule is not None else None,
            )
        elif tool_name == "skill":
            context = replace(context, resolve_skill=self.resolve_skill)
        elif tool_name == "lsp" and self.lsp_request is not None:
            context = replace(context, lsp_request=bind_lsp_request(self.lsp_request, workspace=self.workspace))
        elif tool_name.startswith("mcp/") and self.mcp_request is not None:
            context = replace(context, mcp_request=bind_mcp_request(self.mcp_request, call=invocation.tool_call, workspace=self.workspace))
        elif tool_name == "task" and self.task_command is not None:
            context = replace(context, task_runtime=bind_tool_command(self.task_command, call=invocation.tool_call, context=context))
        elif tool_name == "task_batch" and self.task_batch_command is not None:
            context = replace(context, task_batch_runtime=bind_tool_command(self.task_batch_command, call=invocation.tool_call, context=context))
        elif tool_name == "background_process" and self.process_command is not None:
            context = replace(context, process_runtime=bind_tool_command(self.process_command, call=invocation.tool_call, context=context))
        if tool_timeout is not None and isinstance(tool, RuntimeTimeoutAwareTool):
            return tool.invoke_with_runtime_timeout(invocation.tool_call, context=context, timeout_seconds=tool_timeout)
        return tool.invoke(invocation.tool_call, context=context)

    def _invoke_with_progress(
        self,
        *,
        tool: Tool,
        invocation: ToolInvocation,
        tool_timeout: int | None,
    ) -> Generator[ToolExecutionProgress, None, ToolResult | Exception]:
        tool_call = invocation.tool_call
        progress_queue: queue.Queue[_ToolQueueItem] = queue.Queue(maxsize=_PROGRESS_QUEUE_MAX_ITEMS)
        dropped = _ProgressDropTracker()
        invocation_id = invocation.context.invocation_id or tool_call.tool_call_id
        run_id = invocation.context.run_id
        # One cancellation view per invocation: the interrupt path cancels the
        # run-scoped signal, the timeout path cancels only this invocation.
        cancel_signal = _InvocationCancelSignal(invocation.context.abort_signal)
        next_fallback_ordinal = 1

        def emit_tool_progress(payload: Mapping[str, object]) -> None:
            nonlocal next_fallback_ordinal
            progress_payload: dict[str, object] = {
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

        # Runtime-owned commits made from inside a tool (``background_process``
        # ``op=start`` registering a process row, a delegated ``task`` dispatch,
        # ...) must be attributed to the execution that invoked the tool, not to
        # a thread the executor owns. The tool worker therefore inherits the
        # caller's execution lease, so the storage gateway refuses those commits
        # once ownership is revoked — without this, a seized execution could keep
        # mutating truth through its tools. An unbound caller (foreground run,
        # control plane) leaves the worker unbound. See the "Execution ownership"
        # invariant in docs/contracts/background-task-delegation.md.
        caller_lease = EXECUTION_OWNERSHIP.bound_lease()

        def invoke_tool() -> None:
            with EXECUTION_OWNERSHIP.bind(caller_lease):
                try:
                    result = self._invoke_tool(
                        tool=tool,
                        invocation=invocation,
                        tool_timeout=tool_timeout,
                        emit_tool_progress=emit_tool_progress,
                        cancel_signal=cancel_signal,
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
        runtime_timeout = tool_timeout if tool_timeout is not None and not isinstance(tool, RuntimeTimeoutAwareTool) else None
        deadline = time.monotonic() + runtime_timeout if runtime_timeout is not None else 0.0
        pending_runtime_timeout: int | None = None
        while terminal_item is None:
            try:
                poll_timeout = _PROGRESS_POLL_SECONDS
                if runtime_timeout is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        cancel_signal.set_cancelled(True, reason=_timeout_cancellation_reason(runtime_timeout))
                        pending_runtime_timeout = runtime_timeout
                        break
                    poll_timeout = min(poll_timeout, remaining)
                item = progress_queue.get(timeout=poll_timeout)
            except queue.Empty:
                if invocation.context.abort_signal is not None and invocation.context.abort_signal.cancelled:
                    reason = invocation.context.abort_signal.reason
                    terminal_item = _ToolExceptionItem(RuntimeError(reason if isinstance(reason, str) else "run interrupted"))
                    break
                if runtime_timeout is not None and time.monotonic() >= deadline:
                    cancel_signal.set_cancelled(True, reason=_timeout_cancellation_reason(runtime_timeout))
                    pending_runtime_timeout = runtime_timeout
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

        origin_timeout: RuntimeToolTimeoutError | None = None
        if isinstance(terminal_item, _ToolExceptionItem) and isinstance(terminal_item.exception, RuntimeToolTimeoutError):
            origin_timeout = terminal_item.exception
        if pending_runtime_timeout is not None or origin_timeout is not None:
            # Timeout teardown. The invocation was cancelled above (or timed
            # itself out), so the runtime may only wait a bounded window for the
            # worker to stop, and it must never commit a late result.
            worker.join(timeout=_TOOL_TIMEOUT_REAP_SECONDS)
            execution_stopped = not worker.is_alive()
            late_item = _drain_terminal_item(progress_queue)
            if late_item is not None:
                _log_late_completion(tool_name=tool_call.tool_name, invocation_id=invocation_id, item=late_item)
            if not execution_stopped:
                threading.Thread(
                    target=_observe_abandoned_execution,
                    kwargs={
                        "worker": worker,
                        "progress_queue": progress_queue,
                        "tool_name": tool_call.tool_name,
                        "invocation_id": invocation_id,
                    },
                    name=f"runtime-tool-{tool_call.tool_name}-abandoned",
                    daemon=True,
                ).start()
            if pending_runtime_timeout is not None:
                terminal_item = _ToolExceptionItem(
                    _runtime_timeout_error(
                        tool_name=tool_call.tool_name,
                        timeout_seconds=pending_runtime_timeout,
                        cancellation_signalled=True,
                        execution_stopped=execution_stopped,
                    )
                )
            elif origin_timeout is not None:
                origin_timeout.execution_stopped = execution_stopped
        else:
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
