from __future__ import annotations

import logging
from typing import Literal, cast

from ..hook.config import RuntimeHooksConfig, RuntimeHookSurface
from ..provider.errors import ProviderErrorKind, guidance_for_provider_error_kind
from .contracts import RuntimeStreamChunk
from .events import EventEnvelope
from .session import SessionState
from .session_metadata_helpers import session_with_plan_state

logger = logging.getLogger(__name__)


def hook_failures_are_fatal(hooks: RuntimeHooksConfig | None) -> bool:
    return hooks is not None and hooks.failure_mode == "fail"


def failed_chunk(
    *,
    session: SessionState,
    sequence: int,
    error: str,
    payload: dict[str, object] | None = None,
    status: Literal["failed", "interrupted"] = "failed",
) -> RuntimeStreamChunk:
    failed_session = session_with_plan_state(
        SessionState(
            session=session.session,
            status=status,
            turn=session.turn,
            metadata=session.metadata,
        ),
        status=status,
        error=error,
    )
    failure_payload = {"error": error, **(payload or {})}
    failure_payload = with_runtime_failure_details(failure_payload)
    return RuntimeStreamChunk(
        kind="event",
        session=failed_session,
        event=EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.failed",
            source="runtime",
            payload=failure_payload,
        ),
    )


def lifecycle_hook_failure_chunk(
    *,
    session: SessionState,
    sequence: int,
    surface: RuntimeHookSurface,
    error: str | None,
    hooks: RuntimeHooksConfig | None,
) -> RuntimeStreamChunk | None:
    if error is None:
        return None
    if not hook_failures_are_fatal(hooks):
        logger.warning("%s hook failed for %s: %s", surface, session.session.id, error)
        return None
    return failed_chunk(session=session, sequence=sequence + 1, error=error)


def with_runtime_failure_details(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    error = normalized.get("error")
    if not isinstance(error, str) or not error:
        return normalized
    summary = normalized.get("error_summary")
    if not isinstance(summary, str) or not summary:
        summary = _format_runtime_error_summary(error)
        normalized["error_summary"] = summary
    guidance = normalized.get("retry_guidance")
    if not isinstance(guidance, str) or not guidance:
        retry_guidance = _retry_guidance_for_runtime_failure(normalized)
        if retry_guidance is not None:
            normalized["retry_guidance"] = retry_guidance
    if "error_details" not in normalized:
        details: dict[str, object] = {"message": error, "summary": summary}
        if isinstance(normalized.get("provider_error_kind"), str):
            details["provider_error_kind"] = normalized["provider_error_kind"]
        if isinstance(normalized.get("provider_error_details"), dict):
            details["provider_error_details"] = normalized["provider_error_details"]
        if normalized.get("cancelled") is True:
            details["cancelled"] = True
        normalized["error_details"] = details
    return normalized


def user_interrupted_payload(*, run_id: str | None, reason: str | None) -> dict[str, object]:
    return {
        "kind": "interrupted",
        "cancelled": True,
        "run_id": run_id,
        "reason": reason,
        "retry_guidance": (
            "User interrupted the run. Stop autonomous continuation, preserve current state, "
            "and wait for the user's next instruction before retrying."
        ),
        "diagnostics": [
            {
                "source": "runtime",
                "severity": "info",
                "reason": "user_interrupted",
                "message": "The current run was interrupted by the user.",
            }
        ],
    }


def _retry_guidance_for_runtime_failure(payload: dict[str, object]) -> str | None:
    provider_error_kind = payload.get("provider_error_kind")
    if isinstance(provider_error_kind, str) and provider_error_kind:
        guidance = guidance_for_provider_error_kind(cast(ProviderErrorKind, provider_error_kind))
        if guidance:
            return guidance
    if payload.get("cancelled") is True:
        return "Retry the request if you still want to continue this run."
    if payload.get("kind") == "interrupted":
        return "Retry the request if you want to resume execution after the interruption."
    return None


def _format_runtime_error_summary(error: str) -> str:
    cleaned = error.removeprefix("Error: ").strip()
    if not cleaned:
        return error
    for prefix in ("Runtime failed:", "runtime failed:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned or error


__all__ = [
    "failed_chunk",
    "hook_failures_are_fatal",
    "lifecycle_hook_failure_chunk",
    "user_interrupted_payload",
    "with_runtime_failure_details",
]
