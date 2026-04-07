from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast, final, runtime_checkable
from uuid import uuid4

from ..graph.contracts import GraphEvent, GraphRunRequest
from ..graph.read_only_slice import DeterministicReadOnlyGraph
from ..tools.contracts import Tool, ToolCall, ToolDefinition, ToolResult
from .acp import DisabledAcpAdapter
from .config import RuntimeConfig, load_runtime_config
from .contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk, validate_session_id
from .events import RUNTIME_SKILLS_LOADED, EventEnvelope
from .lsp import DisabledLspManager
from .permission import (
    PendingApproval,
    PermissionPolicy,
    PermissionResolution,
    resolve_permission,
)
from .session import SessionRef, SessionState, SessionStatus, StoredSessionSummary
from .skills import SkillRegistry
from .storage import SessionStore, SqliteSessionStore
from .tool_provider import BuiltinToolProvider


@runtime_checkable
class GraphStep(Protocol):
    @property
    def tool_call(self) -> ToolCall | None: ...

    @property
    def events(self) -> tuple[Any, ...]: ...

    @property
    def output(self) -> str | None: ...

    @property
    def is_finished(self) -> bool: ...


class RuntimeGraph(Protocol):
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep: ...


@dataclass(slots=True)
class ToolRegistry:
    """Small in-memory registry used by the runtime boundary."""

    tools: dict[str, Tool] = field(default_factory=dict)

    @classmethod
    def from_tools(cls, tools: Iterable[Tool]) -> ToolRegistry:
        return cls(tools={tool.definition.name: tool for tool in tools})

    @classmethod
    def with_defaults(cls) -> ToolRegistry:
        return cls.from_tools(BuiltinToolProvider().provide_tools())

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self.tools.values())

    def resolve(self, tool_name: str) -> Tool:
        try:
            return self.tools[tool_name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_name}") from exc


@final
class VoidCodeRuntime:
    """Headless runtime entrypoint for one local deterministic request."""

    _workspace: Path
    _tool_registry: ToolRegistry
    _graph: RuntimeGraph
    _config: RuntimeConfig
    _permission_policy: PermissionPolicy
    _session_store: SessionStore
    _skill_registry: SkillRegistry
    _lsp_manager: DisabledLspManager
    _acp_adapter: DisabledAcpAdapter

    def __init__(
        self,
        *,
        workspace: Path,
        tool_registry: ToolRegistry | None = None,
        graph: RuntimeGraph | None = None,
        config: RuntimeConfig | None = None,
        permission_policy: PermissionPolicy | None = None,
        session_store: SessionStore | None = None,
        skill_registry: SkillRegistry | None = None,
        lsp_manager: DisabledLspManager | None = None,
        acp_adapter: DisabledAcpAdapter | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._tool_registry = tool_registry or ToolRegistry.with_defaults()
        self._graph = graph or DeterministicReadOnlyGraph()
        self._config = config or load_runtime_config(self._workspace)
        self._permission_policy = permission_policy or PermissionPolicy(
            mode=self._config.approval_mode
        )
        self._session_store = session_store or SqliteSessionStore()
        self._skill_registry = skill_registry or self._build_skill_registry()
        self._lsp_manager = lsp_manager or DisabledLspManager(self._config.lsp)
        self._acp_adapter = acp_adapter or DisabledAcpAdapter(self._config.acp)

    def _build_skill_registry(self) -> SkillRegistry:
        skills_config = self._config.skills
        if skills_config is None or skills_config.enabled is not True:
            return SkillRegistry()
        if skills_config.paths:
            return SkillRegistry.discover(
                workspace=self._workspace,
                search_paths=skills_config.paths,
            )
        return SkillRegistry.discover(workspace=self._workspace)

    def _run_with_persistence(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        request = self._validated_request(request)
        events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        try:
            for chunk in self._stream_chunks(request):
                final_session = chunk.session
                if chunk.event is not None:
                    events.append(chunk.event)
                if chunk.kind == "output":
                    if output is not None:
                        raise ValueError("runtime stream emitted multiple output chunks")
                    output = chunk.output
                yield chunk
        except Exception:
            if final_session is not None and final_session.status == "failed":
                response = RuntimeResponse(
                    session=final_session, events=tuple(events), output=output
                )
                self._persist_response(request=request, response=response)
            raise

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
        self._persist_response(request=request, response=response)

    def run(self, request: RuntimeRequest) -> RuntimeResponse:
        events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self._run_with_persistence(request):
            final_session = chunk.session
            if chunk.event is not None:
                events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        return RuntimeResponse(session=final_session, events=tuple(events), output=output)

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        return self._run_with_persistence(request)

    def _stream_chunks(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        session_id = self._resolve_session_id(request)
        session = SessionState(
            session=SessionRef(id=session_id),
            status="running",
            turn=1,
            metadata={"workspace": str(self._workspace), **request.metadata},
        )
        sequence = 1

        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": request.prompt},
            ),
        )

        sequence += 1
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type=RUNTIME_SKILLS_LOADED,
                source="runtime",
                payload={"skills": self._loaded_skill_names()},
            ),
        )

        graph_request = GraphRunRequest(
            session=session,
            prompt=request.prompt,
            available_tools=self._tool_registry.definitions(),
            metadata=request.metadata,
        )
        tool_results: list[ToolResult] = []

        yield from self._execute_graph_loop(
            session=session,
            sequence=sequence,
            graph_request=graph_request,
            tool_results=tool_results,
        )

    def _execute_graph_loop(
        self,
        *,
        session: SessionState,
        sequence: int,
        graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        while True:
            try:
                graph_step = self._graph.step(
                    graph_request,
                    tool_results=tuple(tool_results),
                    session=session,
                )
            except Exception as exc:
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            is_final_step = (
                getattr(graph_step, "is_finished", False)
                or getattr(graph_step, "output", None) is not None
            )
            current_chunk_session = session
            if is_final_step:
                current_chunk_session = SessionState(
                    session=session.session,
                    status="completed",
                    turn=session.turn,
                    metadata=session.metadata,
                )

            for event in self._renumber_events(
                getattr(graph_step, "events", ()),
                session_id=session.session.id,
                start_sequence=sequence + 1,
            ):
                sequence = event.sequence
                yield RuntimeStreamChunk(kind="event", session=current_chunk_session, event=event)

            if is_final_step:
                if getattr(graph_step, "output", None) is not None:
                    yield RuntimeStreamChunk(
                        kind="output",
                        session=current_chunk_session,
                        output=graph_step.output,
                    )
                break

            plan_tool_call = getattr(graph_step, "tool_call", None)
            if plan_tool_call is None:
                yield self._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="graph step did not produce a tool call or output",
                )
                raise ValueError("graph step did not produce a tool call or output")

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="graph.tool_request_created",
                    source="graph",
                    payload={
                        "tool": plan_tool_call.tool_name,
                        "arguments": dict(plan_tool_call.arguments),
                        **(
                            {"path": path}
                            if isinstance((path := plan_tool_call.arguments.get("path")), str)
                            else {}
                        ),
                    },
                ),
            )

            try:
                tool = self._tool_registry.resolve(plan_tool_call.tool_name)
            except Exception as exc:
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_lookup_succeeded",
                    source="runtime",
                    payload={"tool": plan_tool_call.tool_name},
                ),
            )

            sequence += 1
            if approval_resolution is not None:
                pending, decision = approval_resolution
                if (
                    plan_tool_call.tool_name == pending.tool_name
                    and dict(plan_tool_call.arguments) == pending.arguments
                ):
                    sequence += 1
                    permission_chunks = self._approval_resolution_outcome(
                        session=session,
                        pending=pending,
                        decision=decision,
                        sequence=sequence,
                    )
                    approval_resolution = None
                else:
                    msg = (
                        f"graph step produced a different tool call "
                        f"({plan_tool_call.tool_name}) than the pending "
                        f"approval ({pending.tool_name})"
                    )
                    raise ValueError(msg)
            else:
                permission_chunks = self._resolve_permission(
                    session=session,
                    tool=tool.definition,
                    tool_call=plan_tool_call,
                    sequence=sequence,
                )
            yield from permission_chunks.chunks
            if permission_chunks.pending_approval is not None:
                return
            if permission_chunks.denied:
                return

            sequence = permission_chunks.last_sequence

            try:
                tool_result = tool.invoke(plan_tool_call, workspace=self._workspace)
            except Exception as exc:
                yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload=tool_result.data,
                ),
            )

            tool_results.append(tool_result)

    def _failed_chunk(
        self, *, session: SessionState, sequence: int, error: str
    ) -> RuntimeStreamChunk:
        failed_session = SessionState(
            session=session.session,
            status="failed",
            turn=session.turn,
            metadata=session.metadata,
        )
        return RuntimeStreamChunk(
            kind="event",
            session=failed_session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type="runtime.failed",
                source="runtime",
                payload={"error": error},
            ),
        )

    def _persist_response(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None:
        if response.session.status == "waiting":
            pending_approval = self._pending_approval_from_response(response)
            self._session_store.save_pending_approval(
                workspace=self._workspace,
                request=request,
                response=response,
                pending_approval=pending_approval,
            )
            return
        self._session_store.save_run(workspace=self._workspace, request=request, response=response)

    def _resolve_permission(
        self,
        *,
        session: SessionState,
        tool: ToolDefinition,
        tool_call: ToolCall,
        sequence: int,
    ) -> _PermissionOutcome:
        permission = resolve_permission(tool, tool_call, policy=self._permission_policy)
        if tool.read_only:
            return _PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.permission_resolved",
                            source="runtime",
                            payload={"tool": tool_call.tool_name, "decision": "allow"},
                        ),
                    ),
                ),
                last_sequence=sequence,
            )

        pending_approval = permission.pending_approval
        if pending_approval is None:
            raise ValueError("non-read-only permission decisions require pending approval data")
        if permission.decision in ("allow", "deny"):
            return self._approval_resolution_outcome(
                session=session,
                pending=pending_approval,
                decision=permission.decision,
                sequence=sequence,
            )

        pending = pending_approval
        waiting_session = SessionState(
            session=session.session,
            status="waiting",
            turn=session.turn,
            metadata=session.metadata,
        )
        request_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_requested",
            source="runtime",
            payload={
                "request_id": pending.request_id,
                "tool": pending.tool_name,
                "decision": "ask",
                "arguments": pending.arguments,
                "target_summary": pending.target_summary,
                "reason": pending.reason,
                "policy": {"mode": pending.policy_mode},
            },
        )
        return _PermissionOutcome(
            chunks=(
                RuntimeStreamChunk(kind="event", session=waiting_session, event=request_event),
            ),
            last_sequence=sequence,
            pending_approval=pending_approval,
        )

    def _approval_resolution_outcome(
        self,
        *,
        session: SessionState,
        pending: PendingApproval,
        decision: PermissionResolution,
        sequence: int,
    ) -> _PermissionOutcome:
        resolution_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.approval_resolved",
            source="runtime",
            payload={"request_id": pending.request_id, "decision": decision},
        )
        if decision == "deny":
            failed_session = SessionState(
                session=session.session,
                status="failed",
                turn=session.turn,
                metadata=session.metadata,
            )
            failed_event = EventEnvelope(
                session_id=session.session.id,
                sequence=sequence + 1,
                event_type="runtime.failed",
                source="runtime",
                payload={"error": f"permission denied for tool: {pending.tool_name}"},
            )
            return _PermissionOutcome(
                chunks=(
                    RuntimeStreamChunk(kind="event", session=session, event=resolution_event),
                    RuntimeStreamChunk(kind="event", session=failed_session, event=failed_event),
                ),
                last_sequence=sequence + 1,
                denied=True,
            )
        return _PermissionOutcome(
            chunks=(RuntimeStreamChunk(kind="event", session=session, event=resolution_event),),
            last_sequence=sequence,
        )

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        return self._session_store.list_sessions(workspace=self._workspace)

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            return self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        _, response = self._resume_pending_approval_response(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        )
        return response

    def resume_stream(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        validate_session_id(session_id)
        if approval_request_id is None and approval_decision is None:
            response = self._session_store.load_session(
                workspace=self._workspace, session_id=session_id
            )
            yield from self._replay_response(response)
            return
        if approval_request_id is None or approval_decision is None:
            raise ValueError("approval resume requires request id and decision")
        yield from self._resume_pending_approval_stream(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        )

    def _pending_approval_from_response(self, response: RuntimeResponse) -> PendingApproval:
        if not response.events:
            raise ValueError("waiting runtime response must include an approval event")
        approval_event = response.events[-1]
        if approval_event.event_type != "runtime.approval_requested":
            raise ValueError("waiting runtime response must end with approval request")
        payload = approval_event.payload
        raw_policy = cast(dict[str, object], payload.get("policy", {}))
        raw_policy_mode = raw_policy.get("mode", "ask")
        if raw_policy_mode not in ("allow", "deny", "ask"):
            raise ValueError(f"invalid approval policy mode: {raw_policy_mode}")
        return PendingApproval(
            request_id=str(payload["request_id"]),
            tool_name=str(payload["tool"]),
            arguments=cast(dict[str, object], payload.get("arguments", {})),
            target_summary=str(payload.get("target_summary", "")),
            reason=str(payload.get("reason", "")),
            policy_mode=raw_policy_mode,
        )

    def _resume_pending_approval_stream(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_pending_approval_impl(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        )

    def _resume_pending_approval_response(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        stored_events = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        ).events
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self._resume_pending_approval_impl(
            session_id=session_id,
            approval_request_id=approval_request_id,
            approval_decision=approval_decision,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        return stored_events, RuntimeResponse(
            session=final_session,
            events=stored_events + tuple(streamed_events),
            output=output,
        )

    def _resume_pending_approval_impl(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> Iterator[RuntimeStreamChunk]:
        stored = self._session_store.load_session(workspace=self._workspace, session_id=session_id)
        pending = self._session_store.load_pending_approval(
            workspace=self._workspace, session_id=session_id
        )
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")

        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=stored.session.metadata,
        )

        sequence_before_turn = 1
        for event in reversed(stored.events):
            if event.event_type in ("runtime.tool_completed", "runtime.skills_loaded"):
                sequence_before_turn = event.sequence
                break

        max_stored_sequence = stored.events[-1].sequence if stored.events else 0

        tool_results: list[ToolResult] = []
        for event in stored.events:
            if event.event_type == "runtime.tool_completed":
                is_err = "error" in event.payload
                tool_results.append(
                    ToolResult(
                        tool_name=str(event.payload.get("tool", "unknown")),
                        content=str(event.payload.get("content", "")) if not is_err else None,
                        status="error" if is_err else "ok",
                        data=event.payload,
                        error=str(event.payload["error"]) if is_err else None,
                    )
                )

        graph_request = GraphRunRequest(
            session=session,
            prompt=self._prompt_from_events(stored.events),
            available_tools=self._tool_registry.definitions(),
            metadata=session.metadata,
        )

        loop_events: list[EventEnvelope] = []
        output: str | None = None
        try:
            for chunk in self._execute_graph_loop(
                session=session,
                sequence=sequence_before_turn,
                graph_request=graph_request,
                tool_results=tool_results,
                approval_resolution=(pending, approval_decision),
            ):
                if chunk.event is not None:
                    if chunk.event.sequence > max_stored_sequence:
                        loop_events.append(chunk.event)
                        yield chunk
                if chunk.kind == "output":
                    output = chunk.output
                    yield chunk
                session = chunk.session
        except Exception:
            if session.status == "failed":
                response = RuntimeResponse(
                    session=session,
                    events=stored.events + tuple(loop_events),
                    output=output,
                )
                request = RuntimeRequest(
                    prompt=self._prompt_from_events(stored.events), session_id=session_id
                )
                self._persist_response(request=request, response=response)
                return
            raise

        response = RuntimeResponse(
            session=session,
            events=stored.events + tuple(loop_events),
            output=output,
        )

        request = RuntimeRequest(
            prompt=self._prompt_from_events(stored.events), session_id=session_id
        )
        self._persist_response(request=request, response=response)
        return

    @staticmethod
    def _replay_response(response: RuntimeResponse) -> Iterator[RuntimeStreamChunk]:
        for event in response.events:
            yield RuntimeStreamChunk(
                kind="event",
                session=VoidCodeRuntime._replayed_chunk_session(
                    response_session=response.session,
                    event=event,
                ),
                event=event,
            )
        if response.output is not None:
            yield RuntimeStreamChunk(
                kind="output",
                session=VoidCodeRuntime._session_with_status(
                    response.session,
                    "completed"
                    if response.session.status == "completed"
                    else response.session.status,
                ),
                output=response.output,
            )

    @staticmethod
    def _session_with_status(session: SessionState, status: SessionStatus) -> SessionState:
        return SessionState(
            session=session.session,
            status=status,
            turn=session.turn,
            metadata=session.metadata,
        )

    @staticmethod
    def _replayed_chunk_session(
        *, response_session: SessionState, event: EventEnvelope
    ) -> SessionState:
        status: SessionStatus = "running"
        if event.event_type == "runtime.approval_requested":
            status = "waiting"
        elif event.event_type == "runtime.failed":
            status = "failed"
        elif response_session.status == "completed" and (
            event.event_type == "graph.response_ready"
            or (event.event_type == "graph.loop_step" and event.payload.get("phase") == "finalize")
        ):
            status = "completed"
        return VoidCodeRuntime._session_with_status(response_session, status)

    @staticmethod
    def _validated_request(request: RuntimeRequest) -> RuntimeRequest:
        if request.session_id is None:
            return request
        return RuntimeRequest(
            prompt=request.prompt,
            session_id=validate_session_id(request.session_id),
            metadata=request.metadata,
            allocate_session_id=request.allocate_session_id,
        )

    @staticmethod
    def _resolve_session_id(request: RuntimeRequest) -> str:
        if request.session_id is not None:
            return request.session_id
        if request.allocate_session_id:
            return f"session-{uuid4().hex}"
        return "local-cli-session"

    @staticmethod
    def _prompt_from_events(events: tuple[EventEnvelope, ...]) -> str:
        if not events:
            return ""
        prompt = events[0].payload.get("prompt")
        if isinstance(prompt, str):
            return prompt
        return ""

    @staticmethod
    def _renumber_events(
        events: tuple[GraphEvent, ...], *, session_id: str, start_sequence: int
    ) -> tuple[EventEnvelope, ...]:
        return tuple(
            EventEnvelope(
                session_id=session_id,
                sequence=start_sequence + index,
                event_type=event.event_type,
                source=event.source,
                payload=event.payload,
            )
            for index, event in enumerate(events)
        )

    def _loaded_skill_names(self) -> list[str]:
        return sorted(self._skill_registry.skills)


@dataclass(frozen=True, slots=True)
class _PermissionOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    pending_approval: PendingApproval | None = None
    denied: bool = False
