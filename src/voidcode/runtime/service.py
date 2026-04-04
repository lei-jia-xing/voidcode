from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import final

from ..graph.contracts import GraphRunRequest
from ..graph.read_only_slice import DeterministicReadOnlyGraph
from ..tools.contracts import ToolDefinition
from ..tools.read_file import ReadFileTool
from .contracts import RuntimeRequest, RuntimeResponse
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
        session = SessionState(
            session=SessionRef(id=request.session_id or "local-cli-session"),
            status="running",
            turn=1,
            metadata={"workspace": str(self._workspace), **request.metadata},
        )
        events = [
            EventEnvelope(
                session_id=session.session.id,
                sequence=1,
                event_type="runtime.request_received",
                source="runtime",
                payload={"prompt": request.prompt},
            )
        ]

        graph_request = GraphRunRequest(
            session=session,
            prompt=request.prompt,
            available_tools=self._tool_registry.definitions(),
            metadata=request.metadata,
        )
        plan = self._graph.plan(graph_request)
        events.append(
            EventEnvelope(
                session_id=session.session.id,
                sequence=2,
                event_type="graph.tool_request_created",
                source="graph",
                payload={
                    "tool": plan.tool_call.tool_name,
                    "path": plan.tool_call.arguments["path"],
                },
            )
        )

        tool = self._tool_registry.resolve(plan.tool_call.tool_name)
        events.append(
            EventEnvelope(
                session_id=session.session.id,
                sequence=3,
                event_type="runtime.tool_lookup_succeeded",
                source="runtime",
                payload={"tool": plan.tool_call.tool_name},
            )
        )
        events.append(
            EventEnvelope(
                session_id=session.session.id,
                sequence=4,
                event_type="runtime.permission_resolved",
                source="runtime",
                payload={"tool": plan.tool_call.tool_name, "decision": "allow"},
            )
        )

        tool_result = tool.invoke(plan.tool_call, workspace=self._workspace)
        events.append(
            EventEnvelope(
                session_id=session.session.id,
                sequence=5,
                event_type="runtime.tool_completed",
                source="tool",
                payload=tool_result.data,
            )
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
        response = RuntimeResponse(
            session=graph_result.session,
            events=tuple(events) + graph_result.events,
            output=graph_result.output,
        )
        self._session_store.save_run(workspace=self._workspace, request=request, response=response)
        return response

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        return self._session_store.list_sessions(workspace=self._workspace)

    def resume(self, session_id: str) -> RuntimeResponse:
        return self._session_store.load_session(workspace=self._workspace, session_id=session_id)
