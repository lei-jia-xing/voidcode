"""Scoped service Protocols implemented by the runtime and consumed by plugins."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol, runtime_checkable

from .artifacts import (
    ArtifactPage,
    ArtifactReference,
    BackgroundActionStatus,
    ToolAwaitResult,
    ToolInvocationReceipt,
)
from .budgets import PluginBudget, SessionUsageReport
from .common import PAGE_BYTES
from .model_query import StructuredModelResult
from .processes import ProcessHandle, ProcessOutput, ProcessResult, ProcessStream
from .projections import (
    CancelSubtreeReceipt,
    ChildOutcome,
    ChildSendReceipt,
    DescendantNode,
    EventView,
    SessionInfo,
)

if TYPE_CHECKING:
    from ..ask import AskRequestDraft
    from ..foreground import CompositionDraft
    from ..hooks import FactPage
    from ..invocation import ToolContext
    from ..resources import ResourcePage, ResourceReadRequest
    from ..tools import ToolDefinition
    from ..transcript import ContextHistory, TranscriptEntry, TranscriptPage


@runtime_checkable
class BackgroundContext(Protocol):
    """The long-lived handle a background driver keeps across its whole action."""

    @property
    def action_id(self) -> str:
        """The durable continuation identity this driver run was claimed for."""
        ...

    @property
    def payload(self) -> Mapping[str, object]:
        """The immutable JSON object enqueued with the action."""
        ...

    @property
    def session_id(self) -> str:
        """The session whose state and child work this driver acts on."""
        ...

    @property
    def abort_signal(self) -> threading.Event:
        """Set when the owner cancels or the core shuts down; the driver polls it."""
        ...

    @property
    def activation_requested(self) -> threading.Event:
        """Set while a capability activation is trying to take the dispatch fence.

        A long-running driver should stop starting new dispatches (and leave its current
        one promptly) so activation can drain; the signal clears if the attempt is refused.
        """
        ...

    def invoke[T](self, callback: Callable[[ToolContext, PluginServices], T]) -> T:
        """Run `callback` under a fresh owner/revision-bound lease and commit its staged work.

        Each invoke is one atomic commit: state, events, and enqueues land durably before it
        returns, so a crash loses at most the in-flight callback. The services object must not
        escape the callback. When the abort signal is set, invoke raises `InterruptedError`
        and commits nothing.
        """
        ...

    def wait_tool(self, receipt: ToolInvocationReceipt) -> ToolAwaitResult:
        """Wait outside a callback lease for this action's exact deferred tool request.

        The implementation releases the callback/activation guards before waiting. Calling
        this reentrantly from an `invoke` callback is refused rather than deadlocking.
        """
        ...


@runtime_checkable
class StateServices(Protocol):
    """Session and plugin state: lease identity, state CAS, event staging, window projections and overlays."""

    @property
    def owner(self) -> str:
        """The admitted plugin identity bound to this live invocation lease."""
        ...

    def get_state(self, scope: Literal["session", "workspace"] = "session") -> tuple[Mapping[str, object] | None, int]:
        """Fetch current state and version for the bound owner in the given scope."""
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

    def project_window(
        self,
        *,
        covered_from_sequence: int,
        covered_to_sequence: int,
        replacement: str,
    ) -> None:
        """Declare the projection that replaces a committed prefix of this session's window.

        The core verifies the boundary (it must end a completed native batch) and measures the
        accounting itself: the covered bytes are the encoded size of the replaced events, and the
        dropped bytes are what the replacement does not carry. A projection that would grow the
        window is refused. Events are never rewritten; only assembly substitutes the replacement.
        """
        ...

    def window_projection(self) -> Mapping[str, object] | None:
        """The session's current verified projection (coverage, replacement, measured accounting)."""
        ...

    def set_overlay(self, tier: Literal["read-only"] = "read-only") -> None:
        """Tighten the bound session with an owner-owned restrictive overlay.

        Overlays can only restrict; an owner revokes exactly its own row, and the core's
        frozen policy is never affected.
        """
        ...

    def clear_overlay(self) -> bool:
        """Revoke this owner's own overlay; returns whether one was active."""
        ...

    def overlay_active(self) -> bool:
        """Whether this owner's read-only overlay is active on the bound session.

        Reads the same session-overlay truth the core's effective read-only stance uses;
        another owner's overlay and the frozen core policy are not reported here.
        """
        ...

    def session_read_only(self) -> bool:
        """Effective read-only for the bound session: the frozen core policy or any owner overlay."""
        ...


@runtime_checkable
class ChildServices(Protocol):
    """Child sessions and deferred work: spawn, send, join, wait, acknowledge, cancel, lookup, and the continuation queue."""

    def enqueue_continuation(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Enqueue a durable continuation that the session renders as a message of `role`.

        A `user`-role continuation with a string `text` field is rendered verbatim as a user
        message; any other payload is rendered as its JSON under the core's attribution line.
        """
        ...

    def spawn_child(
        self,
        prompt: str,
        *,
        label: str = "",
        tool_allowlist: tuple[str, ...] | None = None,
        max_steps: int | None = None,
        readonly: bool = False,
        notify_on_completion: bool = False,
        idempotency_key: str | None = None,
        result_schema: Mapping[str, object] | None = None,
    ) -> str:
        """Start a delegated child session under this parent session and return its id."""
        ...

    def send_child(
        self,
        child_session_id: str,
        prompt: str,
        *,
        label: str = "",
        notify_on_completion: bool = True,
    ) -> ChildSendReceipt:
        """Send a follow-up prompt to a spawned child session."""
        ...

    def join_child(self, child_session_id: str, *, timeout_seconds: float = 300.0) -> ChildOutcome:
        """Wait for the child session's current run to reach a terminal state."""
        ...

    def wait_children(self, child_session_ids: Sequence[str]) -> tuple[ChildOutcome, ...]:
        """Wait for undelivered terminal outcomes from owned child sessions."""
        ...

    def acknowledge_child_completion(self, child_session_id: str, run_id: str) -> None:
        """Stage delivery receipt for a completed terminal child run."""
        ...

    def cancel_child(self, child_session_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel a child this session spawned, whether it is running or parked for approval."""
        ...

    def list_descendants(self) -> tuple[DescendantNode, ...]:
        """Every transitive descendant of the bound session, in stable creation order."""
        ...

    def cancel_descendant_subtree(
        self,
        target_session_id: str,
        *,
        expected_run_id: str | None,
        reason: str = "owner cancellation",
    ) -> CancelSubtreeReceipt:
        """Request cancellation of a descendant and its whole subtree under a fenced run binding.

        Refused from read-only preparation leases. The target must still carry
        ``expected_run_id`` (None only when it never started); a newer run is a
        stale-target error with zero side effects. The receipt reports which
        sessions accepted the request and which were already terminal; signals
        are not proof the work stopped.
        """
        ...

    def find_child(self, *, idempotency_key: str) -> str | None:
        """The child this parent spawned under `idempotency_key`, if any; never spawns.

        Owner- and parent-bound: a key another owner used is not visible here. This is the
        read-only recovery path for a crash between spawn and the caller's own bookkeeping.
        """
        ...

    def cancel_continuations(self, *, continuation_id: str | None = None) -> None:
        """Stage cancellation of pending or claimed continuations owned by this plugin."""
        ...


@runtime_checkable
class ScheduledWorkServices(Protocol):
    """Durable future work: wakeups and owner background actions."""

    def schedule_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Schedule a future turn of the bound session; the core claims it atomically.

        With an idempotency key the identity is deterministic, so re-scheduling the same future
        point never creates a second wakeup.
        """
        ...

    def cancel_wakeup(self, wakeup_id: str) -> bool:
        """Cancel a pending wakeup owned by this plugin (claimed wakeups are untouched)."""
        ...

    def stage_wakeup(
        self,
        payload: Mapping[str, object],
        *,
        due_at_ms: int,
        idempotency_key: str | None = None,
        role: Literal["system", "user"] = "system",
    ) -> str:
        """Schedule a future turn atomically with the caller's tool-result state and events.

        The row is inserted in the same transaction that commits the callback's
        staged work, so a rejected/aborted commit leaves no orphan wakeup. With an
        idempotency key the identity is deterministic and an already-committed
        row is named instead of duplicated.
        """
        ...

    def stage_cancel_wakeup(self, wakeup_id: str) -> None:
        """Cancel a pending wakeup in the same transaction as the caller's staged work."""
        ...

    def enqueue_background_action(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """Stage one owner background action, run by this plugin's driver outside any model turn.

        The row commits atomically with the caller's result. With `idempotency_key` a retry
        returns the existing action id instead of staging a second action. The driver receives
        a `BackgroundContext` (never this lease) and commits per invoke, so progress is durable
        action by action, not at the end of the run.
        """
        ...

    def cancel_background_action(self, action_id: str, reason: str = "owner cancellation") -> bool:
        """Cancel one owner background action: pending rows are marked cancelled, a live driver is signalled.

        Returns False when the id is unknown or already terminal. A claimed-but-unattended
        (stale) row is marked cancelled without touching any live driver.
        """
        ...

    def background_action_status(self, action_id: str) -> BackgroundActionStatus | None:
        """The durable status of one owner background action, or None when unknown.

        Foreign owners' actions are never visible here. `interrupted` means the claim was lost
        (shutdown or reload); resume is an explicit new action, never an automatic replay.
        """
        ...


class HistoryPageView(Protocol):
    """Structural view of the core history page for SDK signatures.

    The SDK cannot import core (see test_sdk_direction); ``core.history_renderer``
    owns the HistoryPage dataclass and satisfies this shape. Sync is checker-
    enforced: FakeServices subclasses PluginServices, so ty verifies the
    dataclass against this protocol on every override.
    """

    @property
    def text(self) -> str: ...

    @property
    def next_after_sequence(self) -> int: ...

    @property
    def next_offset(self) -> int: ...

    @property
    def truncated(self) -> bool: ...


@runtime_checkable
class HistoryServices(Protocol):
    """Committed-history reads: facts, events, transcripts, sessions and descendants."""

    def read_facts(self, *, after_sequence: int = 0, limit: int = 100) -> FactPage:
        """Read typed committed facts using the same decoder as live observers.

        Limits count source events (1..200). Advance next_sequence even for empty
        pages; has_more, not the fact count, determines whether to continue.
        Recorded facts must satisfy the current contract; incomplete records raise.
        """
        ...

    def read_context_history(self, *, after_sequence: int = 0, limit: int = 600) -> ContextHistory:
        """Read one page of the certified source prefix (1..2000 records) for compaction.

        ``after_sequence`` is exclusive: the page starts at the first source record after
        it. Pages preserve event order and whole records; every ``safe_boundary`` certifies
        the prefix from the session's first source record through that sequence, never a
        page suffix. ``reached_end`` means the page reaches the session's current last
        record. Advance with the last record's sequence; an empty page that is not at the
        end is a contract violation. The core revalidates any declared projection
        independently when it is submitted.
        """
        ...

    def read_transcript(self, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        """Read committed conversation records, scoped to the calling session.

        ``limit`` bounds source events (1..200). The page has a soft 64 KiB
        source-payload budget: an oversized first record is returned whole on
        its own so every message remains reachable. Advance the source cursor
        even on empty pages. This does not apply the current window projection.
        """
        ...

    def latest_event(self, event_type: str, run_id: str | None = None) -> TranscriptEntry | None:
        """Newest decoded conversation record of one event type in the calling session.

        A reverse lookup for callers that need the latest record of a kind without
        paging the whole transcript. Returns None when the session has no such
        event; a stored payload that no longer decodes fails loudly like any read.
        """
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

    def read_child_history(
        self,
        child_session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
    ) -> HistoryPageView:
        """Render one bounded Markdown transcript page for an owned child session."""
        ...

    def read_session_history(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
        page_bytes: int = PAGE_BYTES,
    ) -> HistoryPageView:
        """Render one bounded Markdown transcript page for the bound session or an owned descendant.

        ``page_bytes`` bounds the rendered text (1..40960); callers needing a
        smaller presentation page pass a smaller budget and follow the returned
        cursor. The default preserves the existing 40 KiB page.
        """
        ...


@runtime_checkable
class ToolServices(Protocol):
    """Capability invocation and discovery: tools, plugin queries and model turns."""

    def tool_definition(self, name: str) -> ToolDefinition | None:
        """The definition of ``name`` from the active revision's dispatch snapshot, with this
        session's visibility applied.

        Answered by the core, never copied: it is the same snapshot and the same predicate
        dispatch advertises with, so the reserved core surface, the session's profile scope, and
        the effective read-only stance are all honored without a second catalog. ``None`` covers
        both an unknown name and a name this session may not see.
        """
        ...

    def query(self, name: str, arguments: Mapping[str, object] | None = None) -> Mapping[str, object] | None:
        """Ask the runtime for a named query another admitted plugin answers.

        Queries are runtime-local, read-only conveniences between trusted plugins (see
        ``PluginQuery``): the answering handler runs in this runtime process, receives the caller's
        lease-provided session and workspace, and returns one bounded JSON object or ``None`` for
        "no answer". No core governance is involved, so a query answers questions, never
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
        """Stage an invocation of another tool through the session's admission pipeline."""
        ...

    def model_query(self, prompt: str, *, model: str | None = None, max_output_tokens: int | None = None, include_context: bool = False) -> str:
        """One tool-free provider turn; optionally read the current context without extending it.

        The provider's actual usage is charged once to this session's usage report
        and any active owner budget. An exhausted budget refuses the call before
        dispatch, missing usage stays unknown, and a usage fact that cannot be
        recorded fails the call. The invoking lease's abort signal cancels the
        provider request; a cancelled turn raises ``ModelQueryCancelled`` and never
        returns an answer. Typed failures also cover ``ModelQueryBudgetExhausted``,
        ``ModelQueryContextFailed``, ``ModelQueryUnavailable``,
        ``ModelQueryModelUnavailable``, ``ModelQueryPromptTooLong``, and
        ``ModelQueryToolCalls``; callers classify by type, never message text.
        """
        ...

    def model_query_structured(
        self,
        prompt: str,
        *,
        result_schema: Mapping[str, object],
        model: str | None = None,
        max_output_tokens: int | None = None,
        include_context: bool = False,
    ) -> StructuredModelResult:
        """One tool-free provider turn parsed and validated against ``result_schema``.

        Same governance, budget, usage, and cancellation as ``model_query``: the call
        enters the same dispatch path and usage accounting. ``result_schema`` is a
        JSON Schema (Draft 2020-12) describing one JSON object; the returned
        ``StructuredModelResult`` carries either validated ``data`` or a callable
        ``parse_error`` — never silent success. Cancellation, budget exhaustion,
        and provider errors propagate as exceptions, exactly as ``model_query``.
        """
        ...


@runtime_checkable
class ArtifactServices(Protocol):
    """Artifacts and scheme resources: spill/read content-addressed blobs and dispatch registered readers."""

    def read_resource(self, request: ResourceReadRequest) -> ResourcePage | None:
        """Dispatch one ``scheme://`` URI to its registering provider's reader.

        Runs synchronously inside this invocation under a provider-owner,
        read-only sub-lease (caller's session/workspace/revision). Returns
        ``None`` when the provider knows no such resource. An unregistered
        scheme raises ``ResourceUnavailable``; a malformed page raises
        ``ResourceContractError``; handler bugs and backend failures raise
        visibly and are never confused with "unavailable" or ``None``.
        """
        ...

    def spill_artifact(self, data: bytes | str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactReference:
        """Store bytes under their own content address and return the reference to them.

        The artifact exists (immutably) before any fact that references it commits, so a result
        carrying a reference can never point at missing bytes. Spilling touches only the core's
        own store: a read-only lease may spill output it could not otherwise carry, a hook lease
        (an observer) may not.
        """
        ...

    def spill_artifact_stream(self, chunks: Iterable[bytes], *, media_type: str = "application/octet-stream") -> ArtifactReference:
        """Store a byte stream atomically under its digest.

        The host validates each chunk, enforces its artifact-store bound while writing, and
        discards an incomplete spill on cancellation, revocation, or producer failure. No
        reference is returned until all chunks have been durably published.
        """
        ...

    def read_artifact(
        self,
        sha256: str,
        *,
        offset: int = 0,
        limit: int = 65536,
    ) -> ArtifactPage:
        """Read one byte page by SHA-256 digest; URI parsing belongs to the caller.

        Raises ``FileNotFoundError`` when the digest is well-formed but not
        stored; malformed digests, bad bounds, and storage failures raise
        ``ValueError``/``OSError`` and must never be treated as absence.
        """
        ...


@runtime_checkable
class SuspendServices(Protocol):
    """Suspension intents: foreground composition and ask waits."""

    def suspend_foreground_composition(self, draft: CompositionDraft, *, idempotency_key: str | None = None) -> NoReturn:
        """Stage this composition's wait-intent on the lease and raise SuspendComposition (never returns).

        Tool-callback leases only; hook/command/background leases are refused
        visibly. With idempotency_key a retried stage returns the existing
        composition instead of a second one (spawn_child precedent). Refused
        with CompositionNestedRefused when the suspending lease is already
        composition-bound (single composition slot, no stack); suspend_for_ask
        from such a lease remains allowed.
        """
        ...

    def suspend_for_ask(self, draft: AskRequestDraft, *, idempotency_key: str | None = None) -> NoReturn:
        """Validate refs/shape on the lease, stage the ask intent, raise SuspendAsk (never returns).

        Tool-callback leases only. Media digests are shape-checked here;
        existence is resolved by the plugin via read_artifact BEFORE suspending
        (FileNotFoundError = visible stage error, no park).
        """
        ...


@runtime_checkable
class ProcessServices(Protocol):
    """Child process lifecycle: run, query, spawn, I/O, cancel and list."""

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[ProcessStream, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one child process synchronously in its own process group under this lease.

        Raw byte capture is not implemented by the runtime; ``ProcessResult.raw`` and
        ``RawStreamCapture`` remain the shape a future capture implementation must use.
        """
        ...

    def run_query_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | str | None = None,
        timeout_seconds: float | None = None,
        max_output_bytes: int = 262144,
        on_output: Callable[[ProcessStream, bytes], None] | None = None,
    ) -> ProcessResult:
        """Run one read-only query child process under this lease."""
        ...

    def spawn_service_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        """Start a supervised service process tied to the lifetime of this runtime generation."""
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
        """Start a long-lived supervised process that outlives the current invocation."""
        ...

    def write_process(self, process_id: str, data: bytes | str) -> int:
        """Write to a supervised process's stdin; returns the bytes accepted.

        Only for processes started with `stdin=True` in this runtime: a process that survived a
        core restart keeps running but its pipe did not, so that write is refused rather than lost.
        The core bounds the write and honors the lease's cancellation, so a partial count is the
        honest result of a deadline, cancellation, or a pipe the process stopped reading.
        """
        ...

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: ProcessStream = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        """Read one bounded page of a supervised process's log, with its liveness."""
        ...

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        """Stop a supervised process: SIGTERM its group, then SIGKILL; True when it is gone."""
        ...

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        """Every process this runtime supervises for this session, running or already exited."""
        ...


@runtime_checkable
class BudgetServices(Protocol):
    """Metering and owner budgets: usage reports and budget constraints."""

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


@runtime_checkable
class PluginServices(
    StateServices,
    ChildServices,
    HistoryServices,
    BudgetServices,
    ProcessServices,
    ArtifactServices,
    ToolServices,
    ScheduledWorkServices,
    SuspendServices,
    Protocol,
):
    """The full scoped-services union available to a plugin handler during lease callback.

    Plugins depend on facets; only the runtime implements this composition.
    """

    ...
