from __future__ import annotations

from dataclasses import replace

from ...core.turns import TurnRequest, TurnSessionSnapshot
from ..session import SessionState
from ..session_metadata_helpers import runtime_state_run_id


def turn_session_snapshot(session: SessionState) -> TurnSessionSnapshot:
    """Project runtime session truth into the core's minimal read-only view."""
    metadata = dict(session.metadata)
    if session.session.parent_id is not None:
        metadata["parent_session_id"] = session.session.parent_id
    return TurnSessionSnapshot(session_id=session.session.id, metadata=metadata)


def turn_request_for_session(request: TurnRequest, session: SessionState) -> TurnRequest:
    """Bind a runtime session and its genuine correlation id to a turn request."""
    metadata = dict(request.metadata)
    run_id = runtime_state_run_id(session.metadata)
    if run_id is not None:
        metadata["run_id"] = run_id
    return replace(request, session=turn_session_snapshot(session), metadata=metadata, run_id=run_id)
