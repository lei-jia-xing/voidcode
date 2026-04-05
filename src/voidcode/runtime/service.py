from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import final

from ..graph.contracts import GraphRunRequest
from ..graph.read_only_slice import DeterministicReadOnlyGraph
from ..tools.contracts import ToolDefinition
from ..tools.read_file import ReadFileTool
from .contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk
from .events import EventEnvelope
from .session import SessionRef, SessionState, StoredSessionSummary
from .storage import SessionStore, SqliteSessionStore


@dataclass(slots=True)
class ToolRegistry:
    """Small in-memory registry used by the runtime boundary."""

    tools: dict[str, ReadFileTool] = field(default_factory=dict)

    @classmethod
    def with_defaults(cls) -> ToolRegistry:
        read_tool = ReadFileTool()
        return cls(tools={read_tool.definition.name: read_tool})

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self.tools.values())

    def resolve(self, tool_name: str) -> ReadFileTool:
        try:
            return self.tools[tool_name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {tool_name}") from exc


@final
class VoidCodeRuntime:
    """Headless runtime entrypoint for one local deterministic request."""

    _workspace: Path
    _tool_registry: ToolRegistry
    _graph: DeterministicReadOnlyGraph
    _session_store: SessionStore

    def __init__(
        self,
        *,
        workspace: Path,
        tool_registry: ToolRegistry | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self._workspace = workspace.resolve()
        self._tool_registry = tool_registry or ToolRegistry.with_defaults()
        self._graph = DeterministicReadOnlyGraph()
        self._session_store = session_store or SqliteSessionStore()

    def run(self, request: RuntimeRequest) -> RuntimeResponse:
        events: list[EventEnvelope] = []
        output: str | None = None
        final_session: SessionState | None = None

        for chunk in self.run_stream(request):
            final_session = chunk.session
            if chunk.event is not None:
                events.append(chunk.event)
            if chunk.kind == "output":
                if output is not None:
                    raise ValueError("runtime stream emitted multiple output chunks")
                output = chunk.output

        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")

        response = RuntimeResponse(session=final_session, events=tuple(events), output=output)
        self._session_store.save_run(workspace=self._workspace, request=request, response=response)
        return response

    def run_stream(self, request: RuntimeRequest) -> Iterator[RuntimeStreamChunk]:
        session = SessionState(
            session=SessionRef(id=request.session_id or "local-cli-session"),
            status="running",
            turn=1,
            metadata={"workspace": str(self._workspace), **request.metadata},
        )

        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=1,
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
        plan = self._graph.plan(graph_request)
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=2,
                event_type="graph.tool_request_created",
                source="graph",
                payload={
                    "tool": plan.tool_call.tool_name,
                    "path": plan.tool_call.arguments["path"],
                },
            ),
        )

        tool = self._tool_registry.resolve(plan.tool_call.tool_name)
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=3,
                event_type="runtime.tool_lookup_succeeded",
                source="runtime",
                payload={"tool": plan.tool_call.tool_name},
            ),
        )
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=4,
                event_type="runtime.permission_resolved",
                source="runtime",
                payload={"tool": plan.tool_call.tool_name, "decision": "allow"},
            ),
        )

        tool_result = tool.invoke(plan.tool_call, workspace=self._workspace)
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=5,
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
        graph_result = self._graph.finalize(
            graph_request,
            tool_result,
            session=completed_session,
        )
        for event in graph_result.events:
            yield RuntimeStreamChunk(kind="event", session=graph_result.session, event=event)

        if graph_result.output is not None:
            yield RuntimeStreamChunk(
                kind="output",
                session=graph_result.session,
                output=graph_result.output,
            )

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        return self._session_store.list_sessions(workspace=self._workspace)

    def resume(self, session_id: str) -> RuntimeResponse:
        return self._session_store.load_session(workspace=self._workspace, session_id=session_id)
