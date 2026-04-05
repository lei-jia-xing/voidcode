from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast, final

from ..graph.contracts import GraphRunRequest, GraphRunResult
from ..graph.read_only_slice import DeterministicReadOnlyGraph
from ..tools.contracts import Tool, ToolCall, ToolDefinition, ToolResult
from ..tools.read_file import ReadFileTool
from .contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk, validate_session_id
from .events import EventEnvelope
from .permission import (
    PendingApproval,
    PermissionPolicy,
    PermissionResolution,
    resolve_permission,
)
from .session import SessionRef, SessionState, StoredSessionSummary
from .storage import SessionStore, SqliteSessionStore


class GraphPlan(Protocol):
    @property
    def tool_call(self) -> ToolCall: ...


class RuntimeGraph(Protocol):
    def plan(self, request: GraphRunRequest) -> GraphPlan: ...

    def finalize(
        self,
        request: GraphRunRequest,
        tool_result: ToolResult,
        *,
        session: SessionState,
    ) -> GraphRunResult: ...


@dataclass(slots=True)
class ToolRegistry:
    """Small in-memory registry used by the runtime boundary."""

    tools: dict[str, Tool] = field(default_factory=dict)

    @classmethod
    def with_defaults(cls) -> ToolRegistry:
        read_tool = ReadFileTool()
        tools: dict[str, Tool] = {read_tool.definition.name: read_tool}
        return cls(tools=tools)

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
    _permission_policy: PermissionPolicy
    _session_store: SessionStore

    def __init__(
        self,
        *,
        workspace: Path,
        tool_registry: ToolRegistry | None = None,
        graph: RuntimeGraph | None = None,
        permission_policy: PermissionPolicy | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._tool_registry = tool_registry or ToolRegistry.with_defaults()
        self._graph = graph or DeterministicReadOnlyGraph()
        self._permission_policy = permission_policy or PermissionPolicy(mode="ask")
        self._session_store = session_store or SqliteSessionStore()

    def run(self, request: RuntimeRequest) -> RuntimeResponse:
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
        return response

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
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

    def _stream_chunks(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        session = SessionState(
            session=SessionRef(id=request.session_id or "local-cli-session"),
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

        graph_request = GraphRunRequest(
            session=session,
            prompt=request.prompt,
            available_tools=self._tool_registry.definitions(),
            metadata=request.metadata,
        )
        try:
            plan = self._graph.plan(graph_request)
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
                event_type="graph.tool_request_created",
                source="graph",
                payload={
                    "tool": plan.tool_call.tool_name,
                    "arguments": dict(plan.tool_call.arguments),
                    **(
                        {"path": path}
                        if isinstance((path := plan.tool_call.arguments.get("path")), str)
                        else {}
                    ),
                },
            ),
        )

        try:
            tool = self._tool_registry.resolve(plan.tool_call.tool_name)
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
                payload={"tool": plan.tool_call.tool_name},
            ),
        )

        sequence += 1
        permission_chunks = self._resolve_permission(
            session=session,
            tool=tool.definition,
            tool_call=plan.tool_call,
            sequence=sequence,
        )
        yield from permission_chunks.chunks
        if permission_chunks.pending_approval is not None:
            return
        if permission_chunks.denied:
            return

        sequence = permission_chunks.last_sequence

        try:
            tool_result = tool.invoke(plan.tool_call, workspace=self._workspace)
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

        completed_session = SessionState(
            session=session.session,
            status="completed",
            turn=session.turn,
            metadata=session.metadata,
        )
        try:
            graph_result = self._graph.finalize(
                graph_request,
                tool_result,
                session=completed_session,
            )
        except Exception as exc:
            yield self._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
            raise
        for event in graph_result.events:
            yield RuntimeStreamChunk(kind="event", session=graph_result.session, event=event)

        if graph_result.output is not None:
            yield RuntimeStreamChunk(
                kind="output",
                session=graph_result.session,
                output=graph_result.output,
            )

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
        return self._resume_pending_approval(
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

    def _resume_pending_approval(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> RuntimeResponse:
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
        sequence = stored.events[-1].sequence + 1 if stored.events else 1
        permission_outcome = self._approval_resolution_outcome(
            session=session,
            pending=pending,
            decision=approval_decision,
            sequence=sequence,
        )
        new_events = tuple(
            chunk.event for chunk in permission_outcome.chunks if chunk.event is not None
        )
        if permission_outcome.denied:
            response = RuntimeResponse(
                session=permission_outcome.chunks[-1].session,
                events=stored.events + new_events,
                output=None,
            )
            self._session_store.save_run(
                workspace=self._workspace,
                request=RuntimeRequest(
                    prompt=self._prompt_from_events(stored.events), session_id=session_id
                ),
                response=response,
                clear_pending_approval=True,
            )
            return response

        try:
            tool = self._tool_registry.resolve(pending.tool_name)
            tool_call = ToolCall(tool_name=pending.tool_name, arguments=pending.arguments)
            tool_result = tool.invoke(tool_call, workspace=self._workspace)
        except Exception as exc:
            failed_event = self._failed_chunk(
                session=session,
                sequence=permission_outcome.last_sequence + 1,
                error=str(exc),
            ).event
            assert failed_event is not None
            response = RuntimeResponse(
                session=SessionState(
                    session=session.session,
                    status="failed",
                    turn=session.turn,
                    metadata=session.metadata,
                ),
                events=stored.events + new_events + (failed_event,),
                output=None,
            )
            self._session_store.save_run(
                workspace=self._workspace,
                request=RuntimeRequest(
                    prompt=self._prompt_from_events(stored.events), session_id=session_id
                ),
                response=response,
                clear_pending_approval=True,
            )
            return response

        tool_completed_event = EventEnvelope(
            session_id=session.session.id,
            sequence=permission_outcome.last_sequence + 1,
            event_type="runtime.tool_completed",
            source="tool",
            payload=tool_result.data,
        )
        completed_session = SessionState(
            session=session.session,
            status="completed",
            turn=session.turn,
            metadata=session.metadata,
        )
        graph_request = GraphRunRequest(
            session=session,
            prompt=self._prompt_from_events(stored.events),
            available_tools=self._tool_registry.definitions(),
            metadata={
                **session.metadata,
                "response_sequence": permission_outcome.last_sequence + 2,
            },
        )
        try:
            graph_result = self._graph.finalize(
                graph_request, tool_result, session=completed_session
            )
        except Exception as exc:
            failed_event = self._failed_chunk(
                session=session,
                sequence=permission_outcome.last_sequence + 2,
                error=str(exc),
            ).event
            assert failed_event is not None
            response = RuntimeResponse(
                session=SessionState(
                    session=session.session,
                    status="failed",
                    turn=session.turn,
                    metadata=session.metadata,
                ),
                events=stored.events + new_events + (tool_completed_event, failed_event),
                output=None,
            )
            self._session_store.save_run(
                workspace=self._workspace,
                request=RuntimeRequest(
                    prompt=self._prompt_from_events(stored.events), session_id=session_id
                ),
                response=response,
                clear_pending_approval=True,
            )
            return response
        response = RuntimeResponse(
            session=graph_result.session,
            events=stored.events
            + new_events
            + (tool_completed_event,)
            + self._renumber_events(
                graph_result.events,
                session_id=session.session.id,
                start_sequence=tool_completed_event.sequence + 1,
            ),
            output=graph_result.output,
        )
        self._session_store.save_run(
            workspace=self._workspace,
            request=RuntimeRequest(
                prompt=self._prompt_from_events(stored.events), session_id=session_id
            ),
            response=response,
            clear_pending_approval=True,
        )
        return response

    @staticmethod
    def _validated_request(request: RuntimeRequest) -> RuntimeRequest:
        if request.session_id is None:
            return request
        return RuntimeRequest(
            prompt=request.prompt,
            session_id=validate_session_id(request.session_id),
            metadata=request.metadata,
        )

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
        events: tuple[EventEnvelope, ...], *, session_id: str, start_sequence: int
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


@dataclass(frozen=True, slots=True)
class _PermissionOutcome:
    chunks: tuple[RuntimeStreamChunk, ...]
    last_sequence: int
    pending_approval: PendingApproval | None = None
    denied: bool = False
