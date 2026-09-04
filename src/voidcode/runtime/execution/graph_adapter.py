from __future__ import annotations

from ...graph.contracts import GraphRunRequest, GraphSessionSnapshot
from ..session import SessionState
from ..session_metadata_helpers import runtime_state_run_id


def graph_session_snapshot(session: SessionState) -> GraphSessionSnapshot:
    """Project runtime session truth into the graph's minimal read-only view."""
    metadata = dict(session.metadata)
    if session.session.parent_id is not None:
        metadata["parent_session_id"] = session.session.parent_id
    return GraphSessionSnapshot(session_id=session.session.id, metadata=metadata)


def graph_request_for_session(request: GraphRunRequest, session: SessionState) -> GraphRunRequest:
    """Bind a runtime session and its resolved correlation id to a graph request."""
    metadata = dict(request.metadata)
    run_id = runtime_state_run_id(session.metadata)
    if run_id is not None:
        metadata["run_id"] = run_id
    return GraphRunRequest(
        session=graph_session_snapshot(session),
        prompt=request.prompt,
        assembled_context=request.assembled_context,
        available_tools=request.available_tools,
        context_window=request.context_window,
        metadata=metadata,
        abort_signal=request.abort_signal,
        stream_event_sink=request.stream_event_sink,
        tool_call_preview=request.tool_call_preview,
    )
