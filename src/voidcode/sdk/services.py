"""Scoped runtime services, continuation results, and usage models."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from .json_values import json_wire_object, own_json_object

if TYPE_CHECKING:
    from .tools import ToolDefinition


class QueryUnavailable(ValueError):
    """No admitted plugin in this host generation answers the named query."""


@dataclass(frozen=True, slots=True)
class ContinuationResult:
    continuation_id: str
    owner: str
    session_id: str
    status: Literal["pending", "claimed", "completed", "failed", "cancelled"]
    payload: Mapping[str, object]
    run_status: str | None = None
    run_id: str | None = None
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", own_json_object(self.payload))

    def as_payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "continuation_id": self.continuation_id,
            "owner": self.owner,
            "session_id": self.session_id,
            "status": self.status,
            "payload": json_wire_object(self.payload),
        }
        if self.run_status is not None:
            result["run_status"] = self.run_status
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.output is not None:
            result["output"] = self.output
        if self.error is not None:
            result["error"] = self.error
        if self.stop_cause is not None:
            result["stop_cause"] = self.stop_cause
        return result


@dataclass(frozen=True, slots=True)
class EventView:
    """One bounded, read-only view of a committed session event."""

    sequence: int
    event_type: str
    run_id: str
    revision_id: str
    source: str
    actor: str
    payload: Mapping[str, object] | None = None
    payload_truncated: bool = False

    def __post_init__(self) -> None:
        if self.payload is not None:
            object.__setattr__(self, "payload", own_json_object(self.payload))

    def as_payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "source": self.source,
            "actor": self.actor,
            "payload": None if self.payload is None else json_wire_object(self.payload),
            "payload_truncated": self.payload_truncated,
        }


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """Outcome of ONE delegated child run, as observed by its parent.

    ``run_id`` identifies the logical turn this outcome belongs to; a follow-up run is a new
    ``run_id`` in the same child session, so a caller can never mistake an older terminal fact
    for the result of newer work. ``history_sha256`` names the immutable snapshot of the child's
    readable history at that terminal point (``voidcode://artifact/<sha256>``); the live,
    paged view stays ``voidcode://transcript/<child_session_id>``.
    """

    child_session_id: str
    status: Literal["completed", "failed", "interrupted", "cancelled", "running", "waiting"]
    output: str | None = None
    error: str | None = None
    stop_cause: str | None = None
    run_id: str | None = None
    history_sha256: str | None = None
    history_bytes: int | None = None
    history_truncated: bool = False
    #: Bounded preview when the terminal output was too large to cross the host boundary inline.
    output_preview: str | None = None
    #: Full output artifact, when the run produced more than the inline bound.
    output_sha256: str | None = None
    output_bytes: int | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": self.child_session_id,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "stop_cause": self.stop_cause,
        }
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.history_sha256 is not None:
            payload["history_sha256"] = self.history_sha256
            payload["history_bytes"] = self.history_bytes
            payload["history_truncated"] = self.history_truncated
        if self.output_sha256 is not None:
            payload["output_sha256"] = self.output_sha256
            payload["output_bytes"] = self.output_bytes
            payload["output_preview"] = self.output_preview
        return payload


@dataclass(frozen=True, slots=True)
class ChildSendReceipt:
    """Receipt for one message to an owned child session.

    A receipt reports only durable acceptance: ``started`` means a run identity was claimed and
    the turn is part of that run before this method returned; ``queued`` means the message is a
    pending continuation and a later safe boundary will claim it. Neither means the child
    consumed the message or finished; read the outcome with ``join_child`` or the notice.
    """

    child_session_id: str
    delivery: Literal["started", "queued"]
    child_status: str
    run_id: str | None = None
    message_id: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "child_session_id": self.child_session_id,
            "delivery": self.delivery,
            "child_status": self.child_status,
        }
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.message_id is not None:
            payload["message_id"] = self.message_id
        return payload


#: Continuation owner for kernel child messages. Plugin ids match ``[a-z][a-z0-9_-]*``, so the
#: leading ``@`` makes this value unforgeable by any admitted plugin: a plugin cannot own,
#: claim, or cancel these rows through the generic continuation surface.
CHILD_MESSAGE_OWNER = "@kernel/child"


def child_completion_notice_id(parent_session_id: str, child_session_id: str, run_id: str) -> str:
    """The durable identity of one run's completion notice.

    Run-scoped on purpose: keying by the child alone would let an older completion mask a newer
    follow-up run. The kernel publishes under this id and an acknowledgement cancels exactly it.
    """
    return hashlib.sha256("\0".join((CHILD_MESSAGE_OWNER, parent_session_id, child_session_id, run_id)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PluginBudget:
    """Owner-scoped budget constraints for an active goal or task."""

    owner: str
    since_sequence: int = 0
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_elapsed_seconds: float | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "since_sequence": self.since_sequence,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_elapsed_seconds": self.max_elapsed_seconds,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """Identity of one immutable, content-addressed artifact in the kernel's store."""

    sha256: str
    bytes: int
    media_type: str = "application/octet-stream"

    def as_payload(self) -> dict[str, object]:
        return {"sha256": self.sha256, "bytes": self.bytes, "media_type": self.media_type}


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    """One bounded read of an artifact: `data` at `offset`, `eof` when the store ends there."""

    sha256: str
    offset: int
    total_bytes: int
    data: bytes
    eof: bool
    #: The type the artifact was stored with; empty when its producer declared none.
    media_type: str = ""

    @property
    def text(self) -> str:
        """The page decoded UTF-8 (invalid bytes replaced) for callers that page text."""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    """Identity of one supervised background process, as the caller persists it.

    `pid` alone is not an identity: `start_time` is the kernel's own start stamp for that pid, so a
    later reuse of the number can never be mistaken for this process.
    """

    process_id: str
    pid: int
    start_time: str
    stdout_path: str
    stderr_path: str
    running: bool = True
    exit_code: int | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "pid": self.pid,
            "start_time": self.start_time,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "running": self.running,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """One bounded page of a supervised process's log, with its liveness at read time."""

    process_id: str
    stream: str
    offset: int
    total_bytes: int
    data: bytes
    eof: bool
    running: bool

    @property
    def text(self) -> str:
        """The page decoded UTF-8 (invalid bytes replaced) for callers that page text."""
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Outcome of one lease-scoped child process.

    `stdout`/`stderr` are decoded UTF-8 (invalid bytes replaced) and bounded; when a stream was
    cut at the capture bound `truncated` is set, so a caller never mistakes a clipped capture for
    complete output. A process the runtime stopped is reported by `timed_out`/`cancelled`, which
    are separate from the real exit status.
    """

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Neutral facts about one session, read as one row (never a scan).

    `pending_approval` is the parked approval request this session is waiting on, if any, in the
    same shape the kernel reports to a client. `chain_budget`/`chain_reserved` are the caller's
    root action chain's total and spent actions; nested requests consume this same budget.
    """

    session_id: str
    status: str
    run_id: str | None
    revision_id: str
    parent_session_id: str | None
    root_session_id: str
    depth: int
    turn: int
    last_event_sequence: int
    tool_allowlist: tuple[str, ...] | None = None
    pending_approval: Mapping[str, object] | None = None
    chain_budget: int | None = None
    chain_reserved: int | None = None
    budget: PluginBudget | None = None

    def __post_init__(self) -> None:
        if self.pending_approval is not None:
            object.__setattr__(self, "pending_approval", own_json_object(self.pending_approval))
        if self.tool_allowlist is not None:
            object.__setattr__(self, "tool_allowlist", tuple(self.tool_allowlist))

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "run_id": self.run_id,
            "revision_id": self.revision_id,
            "parent_session_id": self.parent_session_id,
            "root_session_id": self.root_session_id,
            "depth": self.depth,
            "turn": self.turn,
            "last_event_sequence": self.last_event_sequence,
            "tool_allowlist": None if self.tool_allowlist is None else list(self.tool_allowlist),
            "pending_approval": None if self.pending_approval is None else json_wire_object(self.pending_approval),
            "chain_budget": self.chain_budget,
            "chain_reserved": self.chain_reserved,
            "budget": None if self.budget is None else self.budget.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class ToolInvocationReceipt:
    """A same-session request staged with the caller's result, never executed inside its RPC.

    The kernel runs it after the callback returns, through ordinary admission and approval.
    Its committed result is available in this session's events under `request_id`.
    """

    request_id: str
    tool_name: str
    depth: int

    def as_payload(self) -> dict[str, object]:
        return {"request_id": self.request_id, "tool_name": self.tool_name, "depth": self.depth}


@dataclass(frozen=True, slots=True)
class SessionUsageReport:
    """Truthful aggregate token and cost usage computed strictly from core facts."""

    session_id: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    uncached_input_tokens: int | None
    total_cost_usd: float | None = None
    has_unknown_usage: bool = False
    turn_count: int = 0
    elapsed_seconds: float | None = None
    since_sequence: int = 0
    until_sequence: int | None = None
    through_sequence: int = 0

    def as_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "total_cost_usd": self.total_cost_usd,
            "has_unknown_usage": self.has_unknown_usage,
            "turn_count": self.turn_count,
            "elapsed_seconds": self.elapsed_seconds,
            "since_sequence": self.since_sequence,
            "until_sequence": self.until_sequence,
            "through_sequence": self.through_sequence,
        }


@runtime_checkable
class PluginServices(Protocol):
    """Scoped services available to a plugin handler during lease callback."""

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        """Fetch current state and version for the bound owner in the given scope."""
        ...

    def get_state_schema_version(self, scope: Literal["session", "workspace"] = "session") -> int:
        """Declared state schema version of the bound owner's current row (0 when it has none)."""
        ...

    def replace_state(
        self,
        state: Mapping[str, object],
        expected_version: int,
        scope: Literal["session", "workspace"] = "session",
    ) -> int:
        """Stage optimistic concurrency state replacement for the bound owner."""
        ...

    def stage_event(self, event_type: str, payload: Mapping[str, object]) -> None:
        """Stage a plugin-namespaced event to be committed atomically with this turn."""
        ...

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Enqueue a durable continuation that the session renders as a message of `role`.

        A `user`-role continuation with a string `text` field is rendered verbatim as a user
        message; any other payload is rendered as its JSON under the kernel's attribution line.
        """
        ...

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int = 32,
        readonly: bool = False,
        notify_on_completion: bool = False,
    ) -> str:
        """Start a delegated child session of the bound session; returns its durable session id.

        The child can only be narrower than its parent: depth, the chain's spawn budget, the
        parent's tool scope, and the parent's restrictive stance all bound it. A read-only lease
        may therefore delegate read-only work, but never spawn a writable child; `readonly=True`
        records the child's stance durably on the child session (the kernel owns that record).

        With `notify_on_completion` the kernel delivers one bounded completion notice to this
        session when the child's run reaches a terminal state, even after this turn ends, and
        the child's readable history is spilled to an immutable artifact. The notice is keyed by
        the run, never by the child alone, so a follow-up run cannot be masked by an older one.
        A synchronous join is the normal choice for immediate work and does not need a notice.
        """
        ...

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        """Send a follow-up message to ONE child this session spawned, in its own session.

        This is a SESSION verb, not a filesystem write and not user authority: the child keeps its
        tool scope, its read-only overlay, and the kernel's policy. Only the direct parent may
        send (the kernel checks lineage); siblings, parents, and unrelated sessions are refused.
        A running or approval-parked child queues the message durably and consumes it at its next
        safe boundary; an open child starts a new turn immediately. The receipt says `started` or
        `queued` and never claims consumption or completion.
        """
        ...

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        """Wait for the child's CURRENT run to reach a terminal state (no polling).

        While the child is running or has a queued follow-up, this waits instead of returning an
        older terminal fact. The result carries the run id it belongs to.
        """
        ...

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        """Stage cancellation of this run's pending completion notice on the current invocation.

        The cancellation commits atomically with the successful tool result that consumed the
        outcome, so a crash cannot lose the notice and a concurrent join cannot steal delivery.
        A notice already delivered or claimed is untouched; an unknown run is a no-op.
        """
        ...

    def project_window(
        self,
        *,
        covered_from_sequence: int,
        covered_to_sequence: int,
        replacement: str,
    ) -> None:
        """Declare the projection that replaces a committed prefix of this session's window.

        The kernel verifies the boundary (it must end a completed native batch) and measures the
        accounting itself: the covered bytes are the encoded size of the replaced events, and the
        dropped bytes are what the replacement does not carry. A projection that would grow the
        window is refused. Events are never rewritten; only assembly substitutes the replacement.
        """
        ...

    def window_projection(self) -> Mapping[str, object] | None:
        """The session's current verified projection (coverage, replacement, measured accounting)."""
        ...

    def clear_window_projection(self) -> bool:
        """Drop this session's projection, making the raw window visible again."""
        ...

    def set_overlay(self, tier: Literal["read-only"] = "read-only") -> None:
        """Tighten the bound session with an owner-owned restrictive overlay.

        Overlays can only restrict; an owner revokes exactly its own row, and the kernel's
        frozen policy is never affected. This call requires an authentic user-authority
        invocation (e.g. a `direct_user` command), never a model/agent path.
        """
        ...

    def clear_overlay(self) -> bool:
        """Revoke this owner's own overlay; returns whether one was active."""
        ...

    def schedule_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Schedule a future turn of the bound session; the kernel claims it atomically.

        With an idempotency key the identity is deterministic, so re-scheduling the same future
        point never creates a second wakeup.
        """
        ...

    def cancel_wakeup(self, wakeup_id: str) -> bool:
        """Cancel a pending wakeup owned by this plugin (claimed wakeups are untouched)."""
        ...

    def read_events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        """Read this session's committed events, bounded in count and payload size."""
        ...

    def session_info(self) -> SessionInfo:
        """Facts about the bound session: status, run, revision, lineage, approval, chain budget.

        One row read of this session's own record; the chain budget it reports is the caller's
        root chain, which is what bounds delegation and nested invocation.
        """
        ...

    def read_child_events(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
        include_payload: bool = True,
    ) -> tuple[EventView, ...]:
        """Read a child session's committed events, bounded in count and payload size.

        Restricted to this session's OWN descendants: the kernel walks the target's parent chain
        back to the caller, so a sibling, a parent, an unrelated root, or another workspace's
        session is refused — a descendant of a common root is not enough. Bounds are the same
        as `read_events`.
        """
        ...

    def tool_definition(self, name: str) -> ToolDefinition | None:
        """The definition of ``name`` from the active revision's dispatch snapshot, with this
        session's visibility applied.

        Answered by the kernel, never copied: it is the same snapshot and the same predicate
        dispatch advertises with, so the reserved core surface, the session's profile scope, and
        the effective read-only stance are all honored without a second catalog. ``None`` covers
        both an unknown name and a name this session may not see.
        """
        ...

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None:
        """Ask the host for a named query another admitted plugin answers.

        Queries are host-local, read-only conveniences between trusted plugins (see
        ``PluginQuery``): the answering handler runs in this host process, receives the caller's
        lease-provided session and workspace, and returns one bounded JSON object or ``None`` for
        "no answer". No kernel governance is involved, so a query answers questions, never
        performs work. A name nobody answers raises ``QueryUnavailable``; every other failure
        (handler bug, malformed shape, oversized answer) raises visibly and is never confused
        with "unavailable".
        """
        ...

    def invoke_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        expected_revision: str,
    ) -> ToolInvocationReceipt:
        """Invoke another tool through this session's ordinary admission path.

        The kernel resolves the target in the active revision the caller displayed
        (`expected_revision`), applies the session's tool scope, read-only stance, pre-tool gates,
        declared-effect approval policy, and the target's capabilities exactly as it does for a
        model-requested call. Requests are staged atomically with the caller's result and execute
        only after its callback returns; this receipt never waits on execution or human consent.
        Approvable targets go through the ordinary approval boundary, never a rejection for
        needing approval. Recursion consumes the chain's action budget and carries kernel-counted
        depth. Hook leases cannot invoke.
        """
        ...

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[str, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one child process of this plugin host, in its own process group under this lease.

        Nothing here crosses an RPC: the host owns the process, the bytes, and the bounds, and the
        kernel's own governance already decided whether the calling tool may execute at all. What
        the lease adds is stance: a read-only lease and a hook lease are refused outright, because
        neither may touch the machine. `env` overrides the inherited environment; `cwd` defaults to
        the workspace. Each stream is captured up to `max_output_bytes` and the excess is dropped
        with `truncated` set. A lease cancellation kills the whole group before returning.
        """
        ...

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        """Store bytes under their own content address and return the reference to them.

        The artifact exists (immutably) before any fact that references it commits, so a result
        carrying a reference can never point at missing bytes. Spilling touches only the kernel's
        own store: a read-only lease may spill output it could not otherwise carry, a hook lease
        (an observer) may not.
        """
        ...

    def read_artifact(
        self,
        reference: ArtifactReference | Mapping[str, object] | str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        """Read one bounded page of an artifact; `eof` when the page reaches the end."""
        ...

    def spawn_service_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        """Start a supervised *service* process: a helper this host generation owns end to end.

        Same identity, stdin, log paging, and cancellation surface as `spawn_process` (`write_process`,
        `read_process_output`, `cancel_process`, `list_processes` all address it by its id), with one
        lifetime difference: a service process belongs to the host generation that started it. It is
        stopped when that generation retires — a host swap/reload, owner shutdown, or a hard host kill
        — because it joins the host's own process group, while a background process keeps its
        documented "survives a host swap" contract. Service processes are the right shape for a
        language server or another interactive helper: they are not a user job the caller persists,
        and a stale one outliving its generation would hold the workspace.
        """
        ...

    def spawn_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bool = False,
        stdout_path: Path | str | None = None,
        stderr_path: Path | str | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        """Start a supervised process that outlives this invocation, and return its identity.

        Supervised means the host owns the live process and its group: `cancel_process` stops the
        whole group, and the owner's shutdown stops every supervised process. What the caller owns
        is the identity — persist `ProcessHandle` (pid + start_time + log paths): after the caller's
        own restart the host no longer supervises that process, and only that identity can tell a
        live process from a recycled pid. Output goes to the log paths you name (created, never
        truncated); pass none and the host spools into its own temporary files, whose paths the
        returned handle carries. A duplicate `process_id` is refused, so a retried start cannot
        silently create a second process.
        """
        ...

    def write_process(self, process_id: str, data: bytes | str) -> int:
        """Write to a supervised process's stdin; returns the bytes accepted.

        Only for processes started with `stdin=True` in this host: a process that survived a host
        restart keeps running but its pipe did not, so that write is refused rather than lost.
        """
        ...

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: Literal["stdout", "stderr"] = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        """Read one bounded page of a supervised process's log, with its liveness."""
        ...

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        """Stop a supervised process: SIGTERM its group, then SIGKILL; True when it is gone."""
        ...

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        """Every process this host supervises for this session, running or already exited."""
        ...

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None) -> str:
        """One governed provider turn with no tools and no history; returns the text output."""
        ...

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel a child this session spawned, whether it is running or parked for approval."""
        ...

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        """Stage cancellation of pending or claimed continuations owned by this plugin."""
        ...

    def get_usage(self, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        """Fetch truthful aggregate token usage and elapsed time, windowed by dispatch sequence."""
        ...

    def get_budget(self) -> PluginBudget | None:
        """Fetch active budget limits for the bound owner."""
        ...

    def set_budget(
        self,
        *,
        since_sequence: int,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
        max_cost_usd: float | None = None,
        max_elapsed_seconds: float | None = None,
    ) -> None:
        """Stage an owner-scoped budget constraint bound to this session and owner from baseline."""
        ...

    def clear_budget(self) -> None:
        """Stage removal of the bound owner's budget constraint."""
        ...
