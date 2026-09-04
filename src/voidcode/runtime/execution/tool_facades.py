"""Runtime-owned adapters for tool-facing URI reads.

These adapters keep tool-facing protocol implementations at the runtime
boundary. They delegate all lookup, persistence, workspace, and lineage
checks to ``VoidCodeRuntime``; they do not create a second orchestration or
security boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from ...tools.contracts import ToolDefinition
from ...tools.runtime_context import current_runtime_tool_context
from ..contracts import UnknownSessionError

if TYPE_CHECKING:
    from ..service import VoidCodeRuntime


@final
class _RuntimeToolCatalogFacade:
    """Adapter exposing the runtime's live registry to tools via the context.

    Tools read on-demand documentation (``voidcode://tool/<name>``) through
    this facade; it always resolves against the current materialization so MCP
    refreshes are reflected immediately.
    """

    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime

    def lookup(self, tool_name: str) -> ToolDefinition | None:
        return self._runtime._tool_catalog_lookup(tool_name)


@final
class _RuntimeArtifactReadFacade:
    """Adapter exposing runtime-owned, session-guarded artifact reads to tools.

    ``voidcode://artifact/<id>`` resolves against the *caller's* session id
    from the active runtime tool context. It reuses the service's own
    ``read_tool_output_artifact`` path, which applies the session/workspace
    guards (``_validate_session_workspace``) and the artifact-path containment
    checks, so the URI can never reach a foreign session's artifact or an
    arbitrary external file.
    """

    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime

    def read_artifact(
        self,
        *,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        context = current_runtime_tool_context()
        if context is None or not context.session_id:
            raise RuntimeError("voidcode://artifact/<id> requires an active runtime tool invocation context")
        result = self._runtime.read_tool_output_artifact(
            session_id=context.session_id,
            artifact_id=artifact_id,
            offset=max(0, offset or 0),
            limit=max(1, limit or 2000),
        )
        if result.get("status") == "artifact_not_found":
            return None
        return result


@final
class _RuntimeTranscriptReadFacade:
    """Adapter exposing lineage-guarded, bounded transcript reads to tools.

    ``voidcode://transcript/<session_id>`` resolves against the *caller's*
    session id from the active runtime tool context. A session may read its
    own transcript or the transcript of a session it spawned: the persisted
    session parent linkage (``SessionRef.parent_id``) or, as a fallback, the
    background-task parent/child linkage. Unrelated sessions are rejected.
    The returned transcript is bounded (default 20 events, clamped 1..100) and
    payload-stripped: per event only ``sequence``, ``event_type``, ``source``.
    """

    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime

    def read_transcript(
        self,
        *,
        session_id: str,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        context = current_runtime_tool_context()
        if context is None or not context.session_id:
            raise RuntimeError("voidcode://transcript/<session_id> requires an active runtime tool invocation context")
        caller_session_id = context.session_id
        bounded_limit = min(max(limit if limit is not None else 20, 1), 100)
        try:
            target = self._runtime._load_session_result(session_id=session_id)
        except UnknownSessionError, ValueError:
            # During a session's own active run the interrupted-checkpoint row
            # has not yet received the final capability snapshot, so the
            # strict session-result load can fail; fall back to the lighter
            # stored-response load for events and session identity.
            try:
                stored = self._runtime._load_stored_response(session_id=session_id)
            except UnknownSessionError, ValueError:
                return None
            session_state = stored.session
            events = stored.events
            status = stored.session.status
            summary = None
        else:
            session_state = target.session
            events = target.transcript
            status = target.status
            summary = target.summary
        if session_id != caller_session_id:
            lineage_ok = session_state.session.parent_id == caller_session_id
            if not lineage_ok:
                task = self._runtime._session_store.load_background_task_by_child_session(
                    workspace=self._runtime._workspace,
                    child_session_id=session_id,
                )
                lineage_ok = task is not None and task.parent_session_id == caller_session_id
            if not lineage_ok:
                return None
        selected = events[:bounded_limit]
        return {
            "session_id": session_id,
            "status": status,
            "summary": summary,
            "last_event_sequence": events[-1].sequence if events else 0,
            "message_limit": bounded_limit,
            "transcript_count": len(selected),
            "transcript_truncated": len(events) > len(selected),
            "transcript": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "source": event.source,
                }
                for event in selected
            ],
        }


__all__ = (
    "_RuntimeArtifactReadFacade",
    "_RuntimeToolCatalogFacade",
    "_RuntimeTranscriptReadFacade",
)
