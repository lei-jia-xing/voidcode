"""In-process client for a locally owned core.

Same typed surface as :class:`CoreRpcClient`, but every call is a direct
``Core`` invocation in this process: no socket, no HTTP framing, no JSON
round-trip. Errors arrive as ``CoreClientError`` with the transport's
``(status, code)`` refusal, so the TUI cannot tell the two clients apart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from voidcode.sdk.client import (
    ArtifactPage,
    CancelReceipt,
    CommandCatalog,
    CommandResult,
    ContinuationResult,
    CoreClientError,
    HealthStatus,
    ResumeSpec,
    RunResult,
    RunSpec,
    SessionFrame,
    SessionRow,
    SessionUsageReport,
    StreamCancellation,
    ViewsSnapshot,
)
from voidcode.sdk.commands import CommandInputSnapshot
from voidcode.sdk.transcript import ContextHistory, TranscriptPage
from voidcode.transport.projection import (
    FOLLOW_POLL_SECONDS,
    FOLLOW_TAIL_LIMIT,
    core_error_status,
    event_payload,
    health_payload,
    model_payload,
    safe_projection_error,
    session_payload,
    validated_session_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from voidcode.core.engine import Core


class CoreLocalClient:
    """Direct client for a core owned by this process."""

    def __init__(self, core: Core, *, workspace: Path) -> None:
        self._core = core
        self._workspace = str(workspace)

    def close(self) -> None:
        """The core is owned by the caller; there is nothing to close."""

    def __enter__(self) -> CoreLocalClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _refusal(self, error: Exception) -> CoreClientError:
        status, code = core_error_status(error)
        if status == 500:
            return CoreClientError("internal server error", status_code=status, code=code)
        return CoreClientError(str(error), status_code=status, code=code)

    def _validated(self, session_id: str) -> str:
        try:
            return validated_session_id(session_id)
        except ValueError as exc:
            raise CoreClientError(str(exc), status_code=400, code="invalid_params") from None

    def health(self) -> HealthStatus:
        config = self._core.config
        return HealthStatus.from_payload(
            health_payload(
                revision_id=self._core.revision_id,
                stage="serving",
                workspace=self._workspace,
                generation=1,
                upgrade_id=None,
                model=model_payload(raw=config.model, configured_effort=config.reasoning_effort),
            )
        )

    def command_catalog(self) -> CommandCatalog:
        try:
            return self._core.command_catalog()
        except Exception as exc:
            raise self._refusal(exc) from None

    def list_sessions(self, limit: int = 100) -> list[SessionRow]:
        try:
            records = self._core.sessions(limit=limit)
        except Exception as exc:
            raise self._refusal(exc) from None
        return [SessionRow.from_payload(session_payload(record)) for record in records]

    def create_session(self, session_id: str | None = None) -> SessionRow:
        if session_id is not None:
            if not session_id:
                raise CoreClientError("session_id must be a non-empty string", status_code=400, code="invalid_params")
            self._validated(session_id)
        try:
            record = self._core.create_session(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        return SessionRow.from_payload(session_payload(record))

    def get_session(self, session_id: str) -> SessionRow:
        self._validated(session_id)
        try:
            record = self._core.session(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        return SessionRow.from_payload(session_payload(record))

    def show(self, session_id: str, events: bool = False) -> Mapping[str, Any]:
        self._validated(session_id)
        try:
            return self._core.show(session_id, events=events)
        except Exception as exc:
            raise self._refusal(exc) from None

    def run(self, session_id: str, spec: RunSpec) -> RunResult:
        self._validated(session_id)
        try:
            result = self._core.run(
                spec.prompt,
                session_id=session_id,
                max_steps=spec.max_steps,
                root_action_budget=spec.root_action_budget,
                images=spec.images,
            )
        except Exception as exc:
            raise self._refusal(exc) from None
        return RunResult.from_payload(result.as_payload())

    def resume(
        self,
        session_id: str,
        spec: ResumeSpec | None = None,
    ) -> RunResult:
        self._validated(session_id)
        active = spec or ResumeSpec()
        try:
            result = self._core.resume(
                session_id,
                approval_request_id=active.approval_request_id,
                decision=cast(Any, active.decision),
                max_steps=active.max_steps,
                extend_root_actions=active.extend_root_actions,
                root_action_grant_id=active.root_action_grant_id,
            )
        except Exception as exc:
            raise self._refusal(exc) from None
        return RunResult.from_payload(result.as_payload())

    def _validate_command_request(self, session_id: str, name: str, expected_revision: str) -> None:
        self._validated(session_id)
        if not name or "/" in name or "\0" in name or len(name) > 128:
            raise CoreClientError(
                "command name must be a non-empty string without '/' or NUL within 128 characters",
                status_code=400,
                code="invalid_params",
            )
        if not expected_revision:
            raise CoreClientError("expected_revision must be the revision the client displayed", status_code=400, code="invalid_params")

    def command_input(
        self, session_id: str, name: str, expected_revision: str, *, arguments: Mapping[str, object] | None = None
    ) -> CommandInputSnapshot:
        self._validate_command_request(session_id, name, expected_revision)
        try:
            return self._core.command_input(session_id, name, expected_revision=expected_revision, arguments=arguments)
        except Exception as exc:
            raise self._refusal(exc) from None

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: Mapping[str, object] | None = None,
        expected_revision: str = "",
    ) -> CommandResult:
        self._validate_command_request(session_id, name, expected_revision)
        try:
            return self._core.invoke_command(session_id, name, dict(arguments or {}), expected_revision=expected_revision)
        except Exception as exc:
            raise self._refusal(exc) from None

    def confirm(
        self,
        session_id: str,
        request_id: str,
        decision: Literal["allow", "deny"],
        expected_revision: str = "",
    ) -> CommandResult:
        self._validated(session_id)
        if decision not in ("allow", "deny"):
            raise CoreClientError("decision must be 'allow' or 'deny'", status_code=400, code="invalid_params")
        if not expected_revision:
            raise CoreClientError("expected_revision must be the revision the client displayed", status_code=400, code="invalid_params")
        try:
            return self._core.confirm_action(session_id, request_id, cast(Any, decision), expected_revision=expected_revision)
        except Exception as exc:
            raise self._refusal(exc) from None

    def views(self, session_id: str) -> ViewsSnapshot:
        self._validated(session_id)
        try:
            return self._core.plugin_views(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None

    def drain(self, session_id: str, *, limit: int = 10) -> list[ContinuationResult]:
        self._validated(session_id)
        try:
            return list(self._core.drain_continuations(session_id, limit=limit))
        except Exception as exc:
            raise self._refusal(exc) from None

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> SessionUsageReport:
        self._validated(session_id)
        try:
            return self._core.usage(session_id, since_sequence=since_sequence, until_sequence=until_sequence)
        except Exception as exc:
            raise self._refusal(exc) from None

    def cancel(self, session_id: str) -> CancelReceipt:
        self._validated(session_id)
        try:
            accepted = self._core.cancel_session(session_id, reason=f"local cancel for session {session_id}")
        except Exception as exc:
            raise self._refusal(exc) from None
        return CancelReceipt(session_id=session_id, cancellation_requested=bool(accepted))

    def transcript(self, session_id: str, *, after_sequence: int = 0, limit: int = 100) -> TranscriptPage:
        self._validated(session_id)
        try:
            return self._core.transcript_page(session_id, after_sequence=after_sequence, limit=limit)
        except Exception as exc:
            raise self._refusal(exc) from None

    def context_history(self, session_id: str, *, limit: int = 100) -> ContextHistory:
        self._validated(session_id)
        try:
            return self._core.context_history(session_id, limit=limit)
        except Exception as exc:
            raise self._refusal(exc) from None

    async def events_stream(
        self,
        session_id: str,
        after_sequence: int = 0,
        *,
        follow: bool = True,
        cancel: StreamCancellation | None = None,
    ) -> AsyncGenerator[SessionFrame]:
        from voidcode.sdk.client import ErrorFrame, EventFrame, HeadFrame, StreamEvent

        self._validated(session_id)
        if cancel is not None:
            task = asyncio.current_task()
            if task is not None:
                cancel.attach(asyncio.get_running_loop(), task)
        try:
            try:
                record = await asyncio.to_thread(self._core.session, session_id)
            except Exception as exc:
                raise self._refusal(exc) from None
            initial: list[StreamEvent] = []
            cursor = after_sequence
            while True:
                chunk = await asyncio.to_thread(self._core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
                if not chunk:
                    break
                for event in chunk:
                    try:
                        initial.append(StreamEvent.from_payload(event_payload(session_id, event)))
                    except Exception as exc:
                        yield ErrorFrame(session_id=session_id, error=safe_projection_error(event, exc))
                        return
                cursor = chunk[-1].sequence
                if len(chunk) < FOLLOW_TAIL_LIMIT:
                    break
            yield HeadFrame(session=SessionRow.from_payload(session_payload(record)), events=tuple(initial))
            if not follow:
                return
            while True:
                if cancel is not None and cancel.is_closed:
                    return
                await asyncio.sleep(FOLLOW_POLL_SECONDS)
                while True:
                    try:
                        pending = await asyncio.to_thread(self._core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
                    except Exception as exc:
                        yield ErrorFrame(session_id=session_id, error=f"event read failed: {type(exc).__name__}")
                        return
                    if not pending:
                        break
                    for event in pending:
                        try:
                            frame = EventFrame(session_id=session_id, event=StreamEvent.from_payload(event_payload(session_id, event)))
                        except Exception as exc:
                            yield ErrorFrame(session_id=session_id, error=safe_projection_error(event, exc))
                            return
                        cursor = event.sequence
                        yield frame
                    if len(pending) < FOLLOW_TAIL_LIMIT:
                        break
        except asyncio.CancelledError:
            return
        finally:
            if cancel is not None:
                cancel.detach()

    def read_artifact(self, sha256: str, *, offset: int = 0, limit: int = 65536) -> ArtifactPage:
        try:
            return self._core.read_artifact(sha256, offset=offset, limit=limit)
        except Exception as exc:
            raise self._refusal(exc) from None
