"""In-process client for a locally owned core.

Same method surface and frame shapes as :class:`CoreHttpClient`, but every
call is a direct ``Core`` invocation in this process: no socket, no HTTP
framing, no JSON round-trip. Errors arrive as ``CoreClientError`` with the
transport's ``(status, code)`` refusal, so the TUI cannot tell the two
clients apart.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, cast

from voidcode.transport.projection import (
    FOLLOW_POLL_SECONDS,
    FOLLOW_TAIL_LIMIT,
    core_error_status,
    event_payload,
    health_payload,
    session_payload,
    validated_session_id,
)

from .client import CoreClientError, StreamCancellation

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

    def health(self) -> dict[str, Any]:
        return health_payload(
            revision_id=self._core.revision_id,
            stage="serving",
            workspace=self._workspace,
            generation=1,
            upgrade_id=None,
        )

    def command_catalog(self) -> tuple[str, list[dict[str, Any]]]:
        try:
            catalog = self._core.command_catalog()
        except Exception as exc:
            raise self._refusal(exc) from None
        payload = catalog.as_payload()
        commands = cast(list[dict[str, Any]], payload.get("commands", []))
        return str(payload.get("revision_id", "")), list(commands)

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            records = self._core.sessions(limit=limit)
        except Exception as exc:
            raise self._refusal(exc) from None
        return [dict(session_payload(record)) for record in records]

    def create_session(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is not None:
            self._validated(session_id)
        try:
            record = self._core.create_session(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        return dict(session_payload(record))

    def get_session(self, session_id: str) -> dict[str, Any]:
        self._validated(session_id)
        try:
            record = self._core.session(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        return dict(session_payload(record))

    def show(self, session_id: str, events: bool = False) -> dict[str, Any]:
        self._validated(session_id)
        try:
            return dict(self._core.show(session_id, events=events))
        except Exception as exc:
            raise self._refusal(exc) from None

    def run(self, session_id: str, prompt: str, max_steps: int | None = None) -> dict[str, Any]:
        self._validated(session_id)
        steps = 32 if max_steps is None else max_steps
        try:
            result = self._core.run(prompt, session_id=session_id, max_steps=steps)
        except Exception as exc:
            raise self._refusal(exc) from None
        return dict(result.as_payload())

    def resume(
        self,
        session_id: str,
        approval_request_id: str | None = None,
        decision: str | None = None,
        max_steps: int | None = None,
    ) -> dict[str, Any]:
        self._validated(session_id)
        steps = 32 if max_steps is None else max_steps
        if decision is not None and decision not in ("allow", "deny"):
            raise CoreClientError("decision must be 'allow' or 'deny'", status_code=400, code="invalid_params")
        try:
            result = self._core.resume(
                session_id,
                approval_request_id=approval_request_id,
                decision=cast(Any, decision),
                max_steps=steps,
            )
        except Exception as exc:
            raise self._refusal(exc) from None
        return dict(result.as_payload())

    def invoke_command(
        self,
        session_id: str,
        name: str,
        arguments: dict[str, Any] | None = None,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        self._validated(session_id)
        if not name or "/" in name or "\0" in name or len(name) > 128:
            raise CoreClientError(
                "command name must be a non-empty string without '/' or NUL within 128 characters",
                status_code=400,
                code="invalid_params",
            )
        if not expected_revision:
            raise CoreClientError("expected_revision must be the revision the client displayed", status_code=400, code="invalid_params")
        try:
            result = self._core.invoke_command(session_id, name, dict(arguments or {}), expected_revision=expected_revision)
        except Exception as exc:
            raise self._refusal(exc) from None
        return dict(result.as_payload())

    def confirm(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        self._validated(session_id)
        if not expected_revision:
            raise CoreClientError("expected_revision must be the revision the client displayed", status_code=400, code="invalid_params")
        if decision not in ("allow", "deny"):
            raise CoreClientError("decision must be 'allow' or 'deny'", status_code=400, code="invalid_params")
        try:
            result = self._core.confirm_action(session_id, request_id, cast(Any, decision), expected_revision=expected_revision)
        except Exception as exc:
            raise self._refusal(exc) from None
        return dict(result.as_payload())

    def views(self, session_id: str) -> tuple[str, list[dict[str, Any]]]:
        self._validated(session_id)
        try:
            snapshot = self._core.plugin_views(session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        payload = snapshot.as_payload()
        views = cast(list[dict[str, Any]], payload.get("views", []))
        return str(payload.get("revision_id", "")), list(views)

    def drain(self, session_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        self._validated(session_id)
        try:
            results = self._core.drain_continuations(session_id, limit=limit)
        except Exception as exc:
            raise self._refusal(exc) from None
        return [dict(item.as_payload()) for item in results]

    def usage(self, session_id: str, *, since_sequence: int = 0, until_sequence: int | None = None) -> dict[str, Any]:
        self._validated(session_id)
        try:
            report = self._core.usage(session_id, since_sequence=since_sequence, until_sequence=until_sequence)
        except Exception as exc:
            raise self._refusal(exc) from None
        return dict(report.as_payload())

    def cancel(self, session_id: str) -> dict[str, Any]:
        self._validated(session_id)
        try:
            accepted = self._core.cancel_session(session_id, reason=f"local cancel for session {session_id}")
        except Exception as exc:
            raise self._refusal(exc) from None
        return {"cancellation_requested": bool(accepted), "session_id": session_id}

    async def events_stream(
        self,
        session_id: str,
        after_sequence: int = 0,
        *,
        follow: bool = True,
        cancel: StreamCancellation | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        self._validated(session_id)
        if cancel is not None:
            task = asyncio.current_task()
            if task is not None:
                cancel._attach(asyncio.get_running_loop(), task)
        try:
            record = await asyncio.to_thread(self._core.session, session_id)
        except Exception as exc:
            raise self._refusal(exc) from None
        initial: list[dict[str, Any]] = []
        cursor = after_sequence
        while True:
            chunk = await asyncio.to_thread(self._core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
            if not chunk:
                break
            initial.extend(dict(event_payload(session_id, event)) for event in chunk)
            cursor = chunk[-1].sequence
            if len(chunk) < FOLLOW_TAIL_LIMIT:
                break
        yield {"session": dict(session_payload(record)), "events": initial}
        if not follow:
            return
        try:
            while True:
                if cancel is not None and cancel.is_closed:
                    return
                await asyncio.sleep(FOLLOW_POLL_SECONDS)
                while True:
                    try:
                        pending = await asyncio.to_thread(self._core.events_after, session_id, after_sequence=cursor, limit=FOLLOW_TAIL_LIMIT)
                    except Exception as exc:
                        yield {"kind": "error", "session_id": session_id, "error": f"{type(exc).__name__}: {exc}"}
                        return
                    if not pending:
                        break
                    for event in pending:
                        cursor = event.sequence
                        yield {"kind": "event", "session_id": session_id, "event": dict(event_payload(session_id, event))}
                    if len(pending) < FOLLOW_TAIL_LIMIT:
                        break
        except asyncio.CancelledError:
            return
        finally:
            if cancel is not None:
                cancel._detach()
