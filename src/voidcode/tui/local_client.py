"""In-process client for a locally owned core.

Same typed surface as :class:`CoreRpcClient`, but every call is a direct
``Core`` invocation in this process: no socket, no HTTP framing, no JSON
round-trip. Errors arrive as ``CoreClientError`` with the transport's
``(status, code)`` refusal. The TUI distinguishes the arms only through the
capabilities each one declares.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Literal

from voidcode import __version__
from voidcode.capabilities.files.path_queries import PATH_COMPLETION_QUERY
from voidcode.core.storage.store import SessionStatus
from voidcode.core.transcript import transcript_entry
from voidcode.sdk.client import (
    ApprovalPolicyRule,
    ArtifactPage,
    CancelReceipt,
    ClientCapabilities,
    ClientCapability,
    CommandCatalog,
    CommandResult,
    ContinuationResult,
    CoreClientError,
    ErrorFrame,
    EventFrame,
    FollowupListReceipt,
    FollowupWithdrawReceipt,
    HeadFrame,
    HealthModel,
    HealthStatus,
    ResumeSpec,
    RunResult,
    RunSpec,
    SessionClient,
    SessionFrame,
    SessionRow,
    SessionUsageReport,
    StreamCancellation,
    StreamEvent,
    ViewsSnapshot,
)
from voidcode.sdk.commands import CommandInputSnapshot
from voidcode.sdk.services import QueryContractError, QueryUnavailable
from voidcode.sdk.transcript import ContextHistory, TranscriptPage
from voidcode.transport.projection import (
    FOLLOW_TAIL_LIMIT,
    LAUNCH_PROTOCOL_VERSION,
    core_error_status,
    model_payload,
    safe_projection_error,
    validated_session_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from voidcode.core.engine import Core, CoreResult
    from voidcode.core.storage.store import SessionRecord, StoredEvent

#: Idle wake cadence for follow streams: cancel/close recheck only. Commits
#: notify immediately; this timer never drives data (and backstops missed wakes).
_FOLLOW_WAIT_TIMEOUT = 5.0

#: Private pool for commit waits only: asyncio.run shuts down the default
#: executor on stream teardown, so waits must not park there (a cancelled
#: wait lingers invisibly here instead of stalling thread join).
_COMMIT_WAIT_POOL = ThreadPoolExecutor(thread_name_prefix="voidcode-commit-wait")


class CoreLocalClient(SessionClient):
    """Direct client for a core owned by this process."""

    #: Embedded core: local workspace shortening and workspace-config rewriting
    #: via ``/tui model``/``/tui thinking`` are available.
    capabilities = ClientCapabilities(frozenset({ClientCapability.EMBEDDED, ClientCapability.MODEL_SWITCH, ClientCapability.THINKING_SWITCH}))

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

    def _session_row(self, record: SessionRecord) -> SessionRow:
        summary, created_at, updated_at = self._core.store.session_meta(record.session_id)
        raw_status = record.status.value if isinstance(record.status, SessionStatus) else str(record.status)
        return SessionRow(
            session_id=record.session_id,
            workspace=record.workspace,
            status=raw_status,
            active_revision_id=record.active_revision_id,
            run_id=record.run_id,
            turn=record.turn,
            last_event_sequence=record.last_event_sequence,
            summary=summary,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _run_result(result: CoreResult) -> RunResult:
        raw_status = result.status.value if isinstance(result.status, SessionStatus) else result.status
        if raw_status not in ("completed", "waiting", "failed", "interrupted", "queued"):
            raise ValueError(f"unexpected run status: {raw_status}")
        return RunResult(
            session_id=result.session_id,
            run_id=result.run_id,
            revision_id=result.revision_id,
            status=raw_status,
            output=result.output,
            error=result.error,
            pending_approval=result.pending_approval,
            pending_ask=result.pending_ask,
            stop_cause=result.stop_cause,
            budget_exhaustion=result.budget_exhaustion,
        )

    @staticmethod
    def _stream_event(session_id: str, event: StoredEvent) -> StreamEvent:
        return StreamEvent(
            session_id=session_id,
            sequence=event.sequence,
            type=event.event_type,
            run_id=event.run_id,
            revision_id=event.revision_id,
            source=event.source,
            actor=event.actor,
            payload=event.payload,
            transcript=transcript_entry(event),
        )

    def health(self) -> HealthStatus:
        config = self._core.config
        model = model_payload(raw=config.model, configured_effort=config.reasoning_effort)
        return HealthStatus(
            status="ok",
            stage="serving",
            pid=os.getpid(),
            workspace=self._workspace,
            revision_id=self._core.revision_id,
            launch_protocol=LAUNCH_PROTOCOL_VERSION,
            core_version=__version__,
            web_bundle=None,
            generation=1,
            active_upgrade=None,
            model=HealthModel.from_payload(model),
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
        return [self._session_row(record) for record in records]

    def create_session(self, session_id: str | None = None) -> SessionRow:
        if session_id is not None:
            if not session_id:
                raise CoreClientError("session_id must be a non-empty string", status_code=400, code="invalid_params")
            self._validated(session_id)
        try:
            record = self._core.create_session(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        return self._session_row(record)

    def get_session(self, session_id: str) -> SessionRow:
        self._validated(session_id)
        try:
            record = self._core.session(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        return self._session_row(record)

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
                interaction=spec.interaction,
            )
        except Exception as exc:
            raise self._refusal(exc) from None
        return self._run_result(result)

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
                decision=active.decision,
                max_steps=active.max_steps,
                extend_root_actions=active.extend_root_actions,
                root_action_grant_id=active.root_action_grant_id,
                interaction=active.interaction,
                reason=active.reason,
                grant_session_policy=active.grant_session_policy,
            )
        except Exception as exc:
            raise self._refusal(exc) from None
        return self._run_result(result)

    def answer_ask(
        self,
        session_id: str,
        request_id: str,
        values: Mapping[str, object],
        *,
        expected_revision: str,
    ) -> RunResult:
        self._validated(session_id)
        try:
            result = self._core.answer_ask(
                session_id,
                request_id,
                values,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            raise self._refusal(exc) from None
        return self._run_result(result)

    def cancel_ask(
        self,
        session_id: str,
        request_id: str,
        *,
        expected_revision: str,
    ) -> RunResult:
        self._validated(session_id)
        try:
            result = self._core.cancel_ask(
                session_id,
                request_id,
                expected_revision=expected_revision,
            )
        except Exception as exc:
            raise self._refusal(exc) from None
        return self._run_result(result)

    def list_approval_policies(self, session_id: str) -> list[ApprovalPolicyRule]:
        self._validated(session_id)
        try:
            return self._core.list_approval_policies(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None

    def revoke_approval_policy(self, session_id: str, rule_id: str) -> bool:
        self._validated(session_id)
        try:
            return self._core.revoke_approval_policy(session_id, rule_id)
        except Exception as exc:
            raise self._refusal(exc) from None

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
            return self._core.confirm_action(session_id, request_id, decision, expected_revision=expected_revision)
        except Exception as exc:
            raise self._refusal(exc) from None

    def views(self, session_id: str) -> ViewsSnapshot:
        self._validated(session_id)
        try:
            return self._core.plugin_views(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None

    def complete_paths(self, session_id: str, request: Mapping[str, object]) -> Mapping[str, Any] | None:
        """Ask this runtime's files query; query failures stay typed."""
        self._validated(session_id)
        try:
            return self._core.plugin_query(session_id, PATH_COMPLETION_QUERY, dict(request))
        except QueryUnavailable, QueryContractError:
            raise
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

    def list_followups(self, session_id: str) -> FollowupListReceipt:
        self._validated(session_id)
        try:
            return self._core.list_followups(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None

    def withdraw_followup(self, session_id: str, continuation_id: str, expected_revision: str = "") -> FollowupWithdrawReceipt:
        self._validated(session_id)
        if not isinstance(continuation_id, str) or not continuation_id.strip() or "\0" in continuation_id:
            raise CoreClientError("continuation_id must be nonempty text without NUL", status_code=400, code="invalid_params")
        if not expected_revision:
            raise CoreClientError("expected_revision must be the revision the client displayed", status_code=400, code="invalid_params")
        try:
            return self._core.withdraw_followup(session_id, continuation_id, expected_revision=expected_revision)
        except Exception as exc:
            raise self._refusal(exc) from None

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
            generation = self._core.commit_generation
            while True:
                chunk = await asyncio.to_thread(self._core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
                if not chunk:
                    break
                for event in chunk:
                    try:
                        initial.append(self._stream_event(session_id, event))
                    except Exception as exc:
                        yield ErrorFrame(session_id=session_id, error=safe_projection_error(event, exc))
                        return
                cursor = chunk[-1].sequence
                if len(chunk) < FOLLOW_TAIL_LIMIT:
                    break
            yield HeadFrame(session=self._session_row(record), events=tuple(initial))
            if not follow:
                return
            loop = asyncio.get_running_loop()
            while True:
                if cancel is not None and cancel.is_closed:
                    return
                generation = await loop.run_in_executor(_COMMIT_WAIT_POOL, self._core.wait_for_commit, generation, _FOLLOW_WAIT_TIMEOUT)
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
                            frame = EventFrame(session_id=session_id, event=self._stream_event(session_id, event))
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
