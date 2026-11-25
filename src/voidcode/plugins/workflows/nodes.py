"""Node orchestration: spawn, join, park, finish, structured delivery, and cancellation."""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Literal, overload

from voidcode.capabilities.agent_queries import AGENT_DEFINITIONS, AgentDefinitionRequest, apply_agent_profile
from voidcode.sdk.child_schema import canonical_result_schema, validate_child_text
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import (
    ChildOutcome,
    ChildServices,
    PluginServices,
    QueryContractError,
    QueryUnavailable,
    StateConflict,
    StateServices,
    StructuredChildResult,
    StructuredQueryParseError,
)

from .artifacts import _read_full_artifact
from .constants import (
    _ACTIVATION_YIELD_SECONDS,
    _STATE_CONFLICT_ATTEMPTS,
    _TERMINAL_RUN,
    INLINE_OUTPUT_BYTES,
    JOIN_SLICE_SECONDS,
    MAX_CONCURRENT_NODES,
    NodeStatus,
    RunStatus,
)
from .state import WorkflowNode, WorkflowRun, WorkflowState, _clock_ms, _digest, _state_lock, _store


def _commit_lock_factory() -> threading.Lock:
    return threading.Lock()


def _node_semaphore_factory() -> threading.Semaphore:
    return threading.Semaphore(MAX_CONCURRENT_NODES)


def _checked_node_args(
    agent_name: str,
    prompt: str,
    key: str,
    depends_on: Sequence[str],
    tool_allowlist: Sequence[str] | None,
    max_steps: int | None,
) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
    """Validate one node's static arguments; returns (deps, scoped allowlist)."""
    if not isinstance(agent_name, str) or not agent_name:
        raise ValueError(f"node '{key}' agent must be a non-empty agent name")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"node '{key}' prompt must be non-empty text")
    if not isinstance(key, str) or not key:
        raise ValueError("workflow node key must be a non-empty string")
    if isinstance(depends_on, (str, bytes, bytearray)) or not isinstance(depends_on, Sequence):
        raise ValueError(f"node '{key}' depends_on must be a sequence of non-empty keys")
    deps = tuple(depends_on)
    if any(not isinstance(dep, str) or not dep for dep in deps):
        raise ValueError(f"node '{key}' depends_on must list non-empty keys")
    if key in deps:
        raise ValueError(f"node '{key}' cannot depend on itself")
    if tool_allowlist is not None and (
        not isinstance(tool_allowlist, (list, tuple)) or any(not isinstance(name, str) or not name for name in tool_allowlist)
    ):
        raise ValueError(f"node '{key}' tool_allowlist must list non-empty tool names or be null")
    if max_steps is not None and (type(max_steps) is not int or max_steps < 1):
        raise ValueError(f"node '{key}' max_steps must be a positive integer or null")
    return deps, (None if tool_allowlist is None else tuple(tool_allowlist))


@dataclass
class WorkflowContext:
    """The driver handle a trusted workflow script uses for delegated agent nodes.

    This is trusted code, not a sandbox: the script runs as configured plugin code
    with full Python. Every method below runs its core work inside short ``invoke``
    leases and persists before returning; the script itself holds no lease and no commit.
    """

    invoke: Any
    run_id: str
    task: object
    session_id: str = ""
    activation: threading.Event | None = None
    abort: threading.Event | None = None
    _commit_lock: Any = field(default_factory=_commit_lock_factory, repr=False, compare=False)
    _node_slots: Any = field(default_factory=_node_semaphore_factory, repr=False, compare=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False, compare=False)
    _executor_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def stop_nodes(self) -> None:
        """Tell node threads to leave their join loops before the run settles."""
        self._stop.set()

    def close(self) -> None:
        """Abandon any remaining node threads without joining them."""
        self.stop_nodes()
        with self._executor_lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _node_executor(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_NODES, thread_name_prefix="workflow-node")
                weakref.finalize(self, _shutdown_executor, self._executor)
            return self._executor

    async def _run_node(self, function: Any, *args: object) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._node_executor(), function, *args)

    def _raises_if_stopped(self, key: str) -> None:
        if self._stop.is_set():
            raise _AbandonedNode(key)
        if self.abort is not None and self.abort.is_set():
            raise InterruptedError(f"node '{key}' interrupted by driver abort")
        if self.activation is None:
            return
        while self.activation.is_set():
            if self._stop.wait(_ACTIVATION_YIELD_SECONDS):
                raise _AbandonedNode(key)
            if self.abort is not None and self.abort.is_set():
                raise InterruptedError(f"node '{key}' interrupted by driver abort")

    async def agent(
        self,
        agent_name: str,
        prompt: str,
        *,
        key: str,
        depends_on: Sequence[str] = (),
        label: str = "",
        tool_allowlist: Sequence[str] | None = None,
        max_steps: int | None = None,
    ) -> str:
        """Run one named-agent child node, or reuse its durable outcome by key."""
        deps, scoped = _checked_node_args(agent_name, prompt, key, depends_on, tool_allowlist, max_steps)
        return await self._run_node(self._drive_node_guarded, agent_name, prompt, key, deps, label, scoped, max_steps, None)

    async def agent_structured(
        self,
        agent_name: str,
        prompt: str,
        *,
        key: str,
        result_schema: Mapping[str, object],
        depends_on: Sequence[str] = (),
        label: str = "",
        tool_allowlist: Sequence[str] | None = None,
        max_steps: int | None = None,
    ) -> StructuredChildResult:
        """Run one named-agent child node with a declared delivery shape, or reuse it by key.

        The child is instructed to deliver exactly one JSON object matching
        ``result_schema``; the returned reading carries validated ``data`` or a
        callable ``parse_error`` — never a guess. The schema is part of the
        node's effective input: reusing a key requires the same schema, and a
        key must stay on one method (``agent`` vs ``agent_structured``).
        """
        deps, scoped = _checked_node_args(agent_name, prompt, key, depends_on, tool_allowlist, max_steps)
        if not isinstance(result_schema, Mapping):
            raise ValueError(f"node '{key}' result_schema must be an object")
        try:
            canonical_schema = canonical_result_schema(result_schema)
        except ValueError as exc:
            raise ValueError(f"node '{key}' {exc}") from exc
        return await self._run_node(self._drive_node_structured_guarded, agent_name, prompt, key, deps, label, scoped, max_steps, canonical_schema)

    def _drive_node_guarded(
        self,
        agent_name: str,
        prompt: str,
        key: str,
        depends_on: tuple[str, ...],
        label: str,
        tool_allowlist: tuple[str, ...] | None,
        max_steps: int | None,
        result_schema: Mapping[str, object] | None,
    ) -> str:
        with self._node_slots:
            return self._drive_node(agent_name, prompt, key, depends_on, label, tool_allowlist, max_steps, result_schema, structured=False)

    def _drive_node_structured_guarded(
        self,
        agent_name: str,
        prompt: str,
        key: str,
        depends_on: tuple[str, ...],
        label: str,
        tool_allowlist: tuple[str, ...] | None,
        max_steps: int | None,
        result_schema: Mapping[str, object],
    ) -> StructuredChildResult:
        with self._node_slots:
            return self._drive_node(agent_name, prompt, key, depends_on, label, tool_allowlist, max_steps, result_schema, structured=True)

    @overload
    def _drive_node(
        self,
        agent_name: str,
        prompt: str,
        key: str,
        depends_on: tuple[str, ...],
        label: str,
        tool_allowlist: tuple[str, ...] | None,
        max_steps: int | None,
        result_schema: Mapping[str, object] | None,
        *,
        structured: Literal[False],
    ) -> str: ...

    @overload
    def _drive_node(
        self,
        agent_name: str,
        prompt: str,
        key: str,
        depends_on: tuple[str, ...],
        label: str,
        tool_allowlist: tuple[str, ...] | None,
        max_steps: int | None,
        result_schema: Mapping[str, object],
        *,
        structured: Literal[True],
    ) -> StructuredChildResult: ...

    def _drive_node(
        self,
        agent_name: str,
        prompt: str,
        key: str,
        depends_on: tuple[str, ...],
        label: str,
        tool_allowlist: tuple[str, ...] | None,
        max_steps: int | None,
        result_schema: Mapping[str, object] | None,
        *,
        structured: bool,
    ) -> str | StructuredChildResult:

        def stage(context: ToolContext, services: PluginServices) -> _StagedNode:
            return _stage_pending_node(
                context, services, self.run_id, key, depends_on, agent_name, prompt, tool_allowlist, max_steps, result_schema=result_schema
            )

        self._raises_if_stopped(key)
        staged = self._commit(stage)
        if staged.settled is not None:
            if structured:
                # A fingerprint match means the settled node declared this same schema.
                assert staged.settled_structured is not None
                return staged.settled_structured
            return staged.settled
        plan = staged.plan
        assert plan is not None

        self._raises_if_stopped(key)

        def attach(context: ToolContext, services: PluginServices) -> _NodePlan:
            return _attach_node_child(context, services, self.run_id, key, plan, label)

        try:
            node = self._commit(attach).node
        except _ChildNotRecorded as broken:
            # The child exists but the row does not name it. Record it in a separate
            # best-effort commit, then park so a later resume can adopt/join it.
            self._record_child(key, broken.child_id)
            raise _ParkedNode(
                key,
                "interrupted",
                f"child '{broken.child_id}' was created but its node could not be recorded: {broken.cause}",
            ) from broken
        child_id = node.child_session_id
        if child_id is None:
            raise ValueError(f"node '{key}' has no child to drive")
        while True:
            self._raises_if_stopped(key)
            outcome = self.invoke(_JoinNode(child_id, JOIN_SLICE_SECONDS))
            if outcome.status in ("completed", "failed"):
                finished = self._commit(_FinishNode(self.run_id, key, outcome))
                # The node truth already committed inside the lease; control flow
                # raises here so a terminal row is never lost to lease rollback.
                if finished.cancelled:
                    raise _CancelledNode(key)
                if finished.error is not None:
                    raise ValueError(finished.error)
                assert finished.text is not None
                if structured:
                    assert finished.structured is not None
                    return finished.structured
                return finished.text
            if outcome.status in ("waiting", "interrupted"):
                self._commit(_ParkNode(self.run_id, key, outcome))
                raise _ParkedNode(key, outcome.status, outcome.error)
            if outcome.status == "cancelled":
                finished = self._commit(_FinishNode(self.run_id, key, outcome))
                if finished.error is not None:
                    raise ValueError(finished.error)
                if not finished.cancelled:
                    raise AssertionError(f"node '{key}' finish swallowed its cancellation")
                raise _CancelledNode(key)
            # "running" past this slice: release the lease and re-drive.

    def _record_child(self, key: str, child_id: str) -> None:
        """Best-effort durable adoption after a spawn whose row save failed."""

        def adopt(_context: ToolContext, services: PluginServices) -> None:
            state, run, version = _require_run(services, self.run_id)
            if run.status in _TERMINAL_RUN:
                return
            node = run.node(key)
            if node is None or node.child_session_id is not None:
                return
            run = run.with_node(replace(node, status="running", child_session_id=child_id))
            _store(services).save(state.with_run(run), version)

        try:
            self._commit(adopt)
        except Exception:  # noqa: BLE001 - the park reason still names the created child
            return

    def _commit(self, callback: Any) -> Any:
        """One serialized state commit; node/run writers and commands share the session lock."""
        with self._commit_lock, _state_lock(self.session_id):
            for attempt in range(_STATE_CONFLICT_ATTEMPTS):
                try:
                    return self.invoke(callback)
                except StateConflict:
                    if attempt + 1 >= _STATE_CONFLICT_ATTEMPTS:
                        raise
            raise AssertionError("workflow state-conflict retry exhausted without a result")


class _ParkedNode(Exception):
    def __init__(self, key: str, status: str, error: str | None) -> None:
        super().__init__(f"node '{key}' parked: {status}")
        self.key = key
        self.status = status
        self.error = error


class _AbandonedNode(Exception):
    """A node thread left its join loop because the run already settled or stopped."""

    def __init__(self, key: str) -> None:
        super().__init__(f"node '{key}' abandoned")
        self.key = key


class _ChildNotRecorded(Exception):
    """A keyed child was created but its node row could not persist the id."""

    def __init__(self, child_id: str, cause: BaseException) -> None:
        super().__init__(f"child '{child_id}' was created but its node could not be recorded: {cause}")
        self.child_id = child_id
        self.cause = cause


def _shutdown_executor(executor: ThreadPoolExecutor | None) -> None:
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


class _CancelledNode(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(f"node '{key}' cancelled")
        self.key = key


class _PausedRun(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _JoinNode:
    child_session_id: str
    timeout_seconds: float

    def __call__(self, context: ToolContext, services: PluginServices) -> ChildOutcome:
        _ = context
        return services.join_child(self.child_session_id, timeout_seconds=self.timeout_seconds)


@dataclass(frozen=True, slots=True)
class _FinishNode:
    run_id: str
    key: str
    outcome: ChildOutcome

    def __call__(self, context: ToolContext, services: PluginServices) -> _FinishedNode:
        return _finish_node(context, services, self.run_id, self.key, self.outcome)


@dataclass(frozen=True, slots=True)
class _ParkNode:
    run_id: str
    key: str
    outcome: ChildOutcome

    def __call__(self, context: ToolContext, services: PluginServices) -> None:
        _park_node(context, services, self.run_id, self.key, self.outcome)


@dataclass(frozen=True, slots=True)
class _NodePlan:
    node: WorkflowNode


def _require_run(services: StateServices, run_id: str) -> tuple[WorkflowState, WorkflowRun, int]:
    state, version = _store(services).load()
    run = state.runs.get(run_id)
    if run is None:
        raise ValueError(f"unknown workflow run: {run_id!r}")
    return state, run, version


@dataclass(frozen=True, slots=True)
class _EffectivePlan:
    agent_name: str
    body: str
    readonly: bool
    scope: tuple[str, ...] | None
    budget: int | None
    effective_input: str
    result_schema: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _StagedNode:
    node: WorkflowNode | None
    plan: _EffectivePlan | None
    settled: str | None
    settled_structured: StructuredChildResult | None = None


def _resolve_effective_plan(
    services: PluginServices,
    run_id: str,
    key: str,
    depends_on: Sequence[str],
    agent_name: str,
    prompt: str,
    tool_allowlist: tuple[str, ...] | None,
    max_steps: int | None,
    result_schema: Mapping[str, object] | None = None,
) -> _EffectivePlan:
    try:
        profile = AGENT_DEFINITIONS.ask(services, AgentDefinitionRequest(name=agent_name))
    except QueryUnavailable as exc:
        raise ValueError(f"named agents are unavailable; cannot run node '{key}': {exc}") from exc
    except QueryContractError as exc:
        raise ValueError(f"named agent '{agent_name}' answered malformed data: {exc}") from exc
    if profile is None:
        raise ValueError(f"unknown agent: {agent_name!r}")
    body, readonly, scope, budget = apply_agent_profile(profile, prompt, tool_allowlist, max_steps)
    _state, run, _version = _require_run(services, run_id)
    digest_input: dict[str, object] = {
        "body": body,
        "readonly": readonly,
        "scope": None if scope is None else list(scope),
        "budget": budget,
        "depends_on": list(depends_on),
        "input_sha256": run.input_sha256,
    }
    # An undeclared schema is omitted (not hashed as null) so pre-change resumes
    # keep matching; a declared schema alters the fingerprint.
    if result_schema is not None:
        digest_input["result_schema"] = result_schema
    effective_input = _digest(digest_input)
    return _EffectivePlan(
        agent_name=agent_name, body=body, readonly=readonly, scope=scope, budget=budget, effective_input=effective_input, result_schema=result_schema
    )


def _check_topology(run: WorkflowRun, key: str, depends_on: Sequence[str], existing: WorkflowNode | None, effective_input: str) -> None:
    if existing is not None:
        if tuple(existing.depends_on) != tuple(depends_on):
            raise ValueError(f"node '{key}' changed its dependencies; refusing to resume a different graph")
        if existing.effective_input_sha256 is not None and existing.effective_input_sha256 != effective_input:
            raise ValueError(f"node '{key}' changed its effective input; refusing to reuse a different outcome")
        return
    for dep in depends_on:
        dependency = run.node(dep)
        if dependency is None:
            raise ValueError(f"node '{key}' depends on unknown key '{dep}'")
        if dependency.status != "completed":
            raise ValueError(f"node '{key}' depends on '{dep}', which is {dependency.status}")


def _settled_structured(node: WorkflowNode, full_text: str) -> StructuredChildResult | None:
    """Rebuild the structured reading of a completed node; ``None`` when undeclared.

    A declared schema without exactly one persisted reading (or without the
    child that produced it) is a corrupt row: refuse loudly instead of
    re-validating or guessing — the reuse must equal what finish persisted.
    """
    if node.result_schema is None:
        return None
    if (node.data is None) == (node.parse_error is None):
        raise ValueError(f"node '{node.key}' has a declared schema but no single structured reading; refusing to guess it")
    if node.child_session_id is None:
        raise ValueError(f"node '{node.key}' has a structured reading but no child session; refusing to guess it")
    return StructuredChildResult(
        child_session_id=node.child_session_id,
        run_id=node.run_id,
        raw_text=full_text,
        data=node.data,
        parse_error=node.parse_error,
        output_uri=None if node.output_sha256 is None else f"artifact://{node.output_sha256}",
        output_bytes=node.output_bytes,
    )


def _stage_pending_node(
    _context: ToolContext,
    services: PluginServices,
    run_id: str,
    key: str,
    depends_on: Sequence[str],
    agent_name: str,
    prompt: str,
    tool_allowlist: tuple[str, ...] | None,
    max_steps: int | None,
    result_schema: Mapping[str, object] | None = None,
) -> _StagedNode:
    """First short invoke: persist the pending node and its effective spec, spawn nothing."""
    plan = _resolve_effective_plan(services, run_id, key, depends_on, agent_name, prompt, tool_allowlist, max_steps, result_schema=result_schema)
    state, run, version = _require_run(services, run_id)
    if run.status in ("cancelled", "completed", "failed"):
        raise ValueError(f"workflow run '{run_id}' is {run.status}; no new node is accepted")
    if run.status == "paused":
        raise _PausedRun
    existing = run.node(key)
    _check_topology(run, key, depends_on, existing, plan.effective_input)
    if existing is not None:
        if existing.status == "completed" and existing.output_sha256 is not None:
            settled = _read_full_artifact(services, existing.output_sha256)
            return _StagedNode(node=existing, plan=None, settled=settled, settled_structured=_settled_structured(existing, settled))
        if existing.status == "completed" and existing.output is not None:
            return _StagedNode(node=existing, plan=None, settled=existing.output, settled_structured=_settled_structured(existing, existing.output))
        if existing.status == "completed" and existing.output_sha256 is None:
            # Preview-only capture failure persisted by _finish_node: no full text
            # exists to return, and redriving cannot manufacture it. Fail visibly
            # here (nothing staged yet, so raising inside is safe).
            raise ValueError(_unavailable_output_message(key, existing.run_id, existing.output_error))
        if existing.status in ("failed", "cancelled", "skipped"):
            raise ValueError(f"node '{key}' already ended as {existing.status}; refusing to replay it")
        return _StagedNode(node=existing, plan=plan, settled=None)
    node = WorkflowNode(
        key=key,
        status="pending",
        depends_on=tuple(depends_on),
        agent=agent_name,
        effective_input_sha256=plan.effective_input,
        result_schema=plan.result_schema,
    )
    run = replace(run, nodes=(*run.nodes, node), updated_at=_clock_ms())
    _store(services).save(state.with_run(run), version)
    return _StagedNode(node=node, plan=plan, settled=None)


def _attach_node_child(_context: ToolContext, services: PluginServices, run_id: str, key: str, plan: _EffectivePlan, label: str) -> _NodePlan:
    """Second short invoke: create or recover the child, then record its id."""
    state, run, version = _require_run(services, run_id)
    if run.status in ("cancelled", "completed", "failed"):
        raise ValueError(f"workflow run '{run_id}' is {run.status}; no new node is accepted")
    if run.status == "paused":
        raise _PausedRun
    node = run.node(key)
    if node is None:
        raise ValueError(f"node '{key}' has no staged plan to attach")
    _check_topology(run, key, node.depends_on, node, plan.effective_input)
    if node.child_session_id is not None:
        return _NodePlan(node)
    idempotency_key = f"workflow:{run_id}:{key}"
    # A crash (or a lost state CAS) can leave the keyed child without its node row.
    # find_child is a pure read bound to this parent/owner, so adopt before spawning;
    # this also works after a resume that captured a new root action identity.
    existing_child = services.find_child(idempotency_key=idempotency_key)
    if existing_child is not None:
        descendants = {item.session_id: item for item in services.list_descendants()}
        snapshot = descendants.get(existing_child)
        # Adopt only a child that actually started. A pristine reserved-but-unstarted
        # child is repaired by the core's keyed spawn path (same action root), never
        # adopted into a join that could wait on it forever.
        if snapshot is not None and (snapshot.run_id is not None or snapshot.status != "idle"):
            node = replace(node, status="running", child_session_id=existing_child)
            _store(services).save(state.with_run(run.with_node(node)), version)
            return _NodePlan(node)
    # Always spawn under the same key and spec: the core reuses the mapped child,
    # repairs a pristine reserved-but-unstarted one, and refuses a different spec.
    child_id = services.spawn_child(
        plan.body,
        label=label or f"{run.workflow}:{key}",
        tool_allowlist=plan.scope,
        max_steps=plan.budget,
        readonly=plan.readonly,
        notify_on_completion=False,
        idempotency_key=idempotency_key,
        result_schema=plan.result_schema,
    )
    node = replace(node, status="running", child_session_id=child_id)
    try:
        _store(services).save(state.with_run(run.with_node(node)), version)
    except StateConflict:
        # The outer _commit retry re-reads and adopts by key; a commit-stage conflict
        # is recoverable and must not strand the created child.
        raise
    except Exception as exc:
        raise _ChildNotRecorded(child_id, exc) from exc
    return _NodePlan(node)


def _unavailable_output_message(key: str, run_id: str | None, output_error: str | None) -> str:
    cause = output_error or "no full output was captured"
    return f"node '{key}' completed but its full output is unavailable for run '{run_id or 'unknown'}': {cause}"


@dataclass(frozen=True, slots=True)
class _FinishedNode:
    """The committed result of one finish callback; the driver raises control flow outside the lease."""

    node: WorkflowNode
    text: str | None
    error: str | None = None
    cancelled: bool = False
    structured: StructuredChildResult | None = None


def _finish_node(context: ToolContext, services: PluginServices, run_id: str, key: str, outcome: ChildOutcome) -> _FinishedNode:
    """Persist one terminal node outcome; control-flow errors are returned, never raised after staging.

    Core commits a leased callback's staged state only when the callback returns: raising
    after ``replace_state`` revokes the lease and discards the row. Every terminal branch
    below therefore stages the node's durable truth and returns it; the driver raises
    ``ValueError``/``_CancelledNode`` after the commit landed. Only pre-stage validation
    (unknown node/child, nonterminal outcome) raises here. A completed child whose full
    output was not captured keeps its real completed status with the committed preview
    and capture cause; only the ``str`` consumption fails, visibly, outside the lease.
    """
    _ = context
    state, run, version = _require_run(services, run_id)
    node = run.node(key)
    if node is None or node.child_session_id != outcome.child_session_id:
        raise ValueError(f"node '{key}' has no child to finish")
    if run.status in _TERMINAL_RUN:
        # The run already settled; still record this node's own terminal truth so
        # no row keeps lying as running/waiting, then yield to the settled run.
        if outcome.status == "completed":
            terminal: NodeStatus = "completed"
        elif outcome.status == "failed":
            terminal = "failed"
        elif outcome.status == "cancelled":
            terminal = "cancelled"
        elif outcome.status == "interrupted":
            terminal = "interrupted"
        else:
            raise ValueError(f"cannot finish workflow node '{key}' from child status {outcome.status!r}")
        if node.status in ("pending", "running", "waiting", "interrupted"):
            node = replace(
                node,
                status=terminal,
                run_id=outcome.run_id,
                output_preview=outcome.output_preview,
                output_error=outcome.output_error,
                history_error=outcome.history_error,
                error=outcome.error if terminal != "completed" else None,
                budget_exhaustion=outcome.budget_exhaustion,
            )
            _store(services).save(state.with_run(run.with_node(node)), version)
        if run.status == "cancelled":
            return _FinishedNode(node=node, text=None, cancelled=True)
        if run.status == "failed":
            return _FinishedNode(node=node, text=None, error=f"node '{key}' ends with failed run '{run_id}'")
        return _FinishedNode(node=node, text=None, error=f"node '{key}' ends with {run.status} run '{run_id}'")
    if node.status == "completed" and node.output is not None and node.output_sha256 is None:
        return _FinishedNode(node=node, text=node.output, structured=_settled_structured(node, node.output))
    if outcome.status not in ("completed", "failed", "cancelled"):
        raise ValueError(f"cannot finish workflow node '{key}' from child status {outcome.status!r}")
    if outcome.run_id is not None:
        services.acknowledge_child_completion(outcome.child_session_id, outcome.run_id)
    # A sha always wins: outcome.output is a bounded preview when the full bytes spilled.
    # Missing output (None) is not empty output (""): only persist "" when the child
    # genuinely produced no text without a preview or capture cause.
    output: str | None = None
    output_sha256: str | None = outcome.output_sha256
    output_bytes: int | None = outcome.output_bytes
    preview_only = False
    if output_sha256 is None:
        if outcome.output is None:
            if outcome.output_preview is not None or outcome.output_error is not None:
                preview_only = True
            else:
                output = ""
        else:
            text = outcome.output
            if len(text.encode("utf-8")) > INLINE_OUTPUT_BYTES:
                reference = services.spill_artifact(text, media_type="text/plain; charset=utf-8")
                output_sha256 = reference.sha256
                output_bytes = reference.bytes
            else:
                output = text
    if outcome.status == "completed":
        status: NodeStatus = "completed"
    elif outcome.status == "failed":
        status = "failed"
    else:
        status = "cancelled"
    # Read the full bytes BEFORE staging: a missing artifact raises here while
    # nothing is staged, so the node row is never lost to lease rollback.
    text: str | None = None
    if status == "completed" and not preview_only:
        if output_sha256 is not None:
            text = _read_full_artifact(services, output_sha256)
        else:
            text = output
    error = outcome.error if status != "completed" else None
    data: Mapping[str, object] | None = None
    parse_error: StructuredQueryParseError | None = None
    if status == "completed" and not preview_only and text is not None and node.result_schema is not None:
        validated, failure = validate_child_text(node.result_schema, text)
        if failure is not None:
            parse_error = failure
        else:
            assert validated is not None
            data = validated
    node = replace(
        node,
        status=status,
        run_id=outcome.run_id,
        output=output,
        output_sha256=output_sha256,
        output_bytes=output_bytes,
        output_preview=outcome.output_preview,
        output_error=outcome.output_error,
        history_error=outcome.history_error,
        error=error,
        budget_exhaustion=outcome.budget_exhaustion,
        data=data,
        parse_error=parse_error,
    )
    run = run.with_node(node)
    _store(services).save(state.with_run(run), version)
    if status == "failed":
        return _FinishedNode(node=node, text=None, error=f"node '{key}' failed: {error or 'no error reported'}")
    if status == "cancelled":
        return _FinishedNode(node=node, text=None, cancelled=True)
    if preview_only:
        return _FinishedNode(node=node, text=None, error=_unavailable_output_message(key, outcome.run_id, outcome.output_error))
    assert text is not None
    structured: StructuredChildResult | None = None
    if node.result_schema is not None:
        # Exactly one of data/parse_error was persisted above; the constructor enforces it.
        structured = StructuredChildResult(
            child_session_id=outcome.child_session_id,
            run_id=outcome.run_id,
            raw_text=text,
            data=data,
            parse_error=parse_error,
            output_uri=None if node.output_sha256 is None else f"artifact://{node.output_sha256}",
            output_bytes=node.output_bytes,
        )
    return _FinishedNode(node=node, text=text, structured=structured)


def _park_node(context: ToolContext, services: StateServices, run_id: str, key: str, outcome: ChildOutcome) -> None:
    _ = context
    state, run, version = _require_run(services, run_id)
    node = run.node(key)
    if node is None or run.status in _TERMINAL_RUN:
        return
    status: NodeStatus = "interrupted" if outcome.status == "interrupted" else "waiting"
    # The parked run owns this row now: prior-run output and capture data must not linger.
    # The declared schema is spec, not run output, so it stays; its reading is cleared.
    run = run.with_node(
        replace(
            node,
            status=status,
            run_id=outcome.run_id,
            output=None,
            output_sha256=None,
            output_bytes=None,
            output_preview=outcome.output_preview,
            output_error=outcome.output_error,
            history_error=outcome.history_error,
            error=outcome.error,
            budget_exhaustion=outcome.budget_exhaustion,
            data=None,
            parse_error=None,
        )
    )
    _store(services).save(state.with_run(run), version)


def _cancel_run_children(services: ChildServices, run: WorkflowRun, *, reason: str) -> tuple[str, ...]:
    """Cancel every open node child and its subtree; returns best-effort failure notes.

    The run owns only the direct node children; their descendants are owned by those
    children. `cancel_descendant_subtree` is the one mutation that stops the whole
    delegated subtree with a run-bound expectation, so a newer child run is never
    cancelled by a stale workflow row.
    """
    descendants = {node.session_id: node for node in services.list_descendants()}
    failures: list[str] = []
    for node in run.nodes:
        if node.status in ("completed", "failed", "cancelled", "skipped"):
            continue
        child_id = node.child_session_id
        if child_id is None:
            try:
                child_id = services.find_child(idempotency_key=f"workflow:{run.run_id}:{node.key}")
            except (ValueError, RuntimeError) as exc:
                failures.append(f"{node.key}: {exc}")
                continue
        if child_id is None:
            continue
        snapshot = descendants.get(child_id)
        try:
            if snapshot is None:
                services.cancel_child(child_id, reason=reason)
            else:
                services.cancel_descendant_subtree(child_id, expected_run_id=snapshot.run_id, reason=reason)
        except (ValueError, RuntimeError, PermissionError) as exc:
            failures.append(f"{node.key}: {type(exc).__name__}: {exc}")
    return tuple(failures)


def _cancellation_note(failures: Sequence[str]) -> str:
    return "" if not failures else "; cancellation: " + "; ".join(failures[:3])


def _settled_open_nodes(run: WorkflowRun, status: RunStatus) -> WorkflowRun:
    """Rewrite rows left open by a failed/cancelled run as cancelled, never lying `running`."""
    if status not in ("failed", "cancelled"):
        return run
    nodes = tuple(
        replace(node, status="cancelled", budget_exhaustion=None) if node.status in ("pending", "running", "waiting", "interrupted") else node
        for node in run.nodes
    )
    return replace(run, nodes=nodes)
