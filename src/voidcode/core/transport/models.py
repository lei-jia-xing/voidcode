"""HTTP-owned wire models for the core transport.

Request bodies validate at ingress (extra=forbid). Responses reuse the SDK and
storage dataclasses directly as ``response_model`` documentation — including the
``CommandCatalog``/``ViewsSnapshot`` envelopes, which are SDK-owned — and
handlers return their own ``Response`` built from ``as_payload()``, so FastAPI
never re-serializes ``MappingProxy`` payloads. Only list envelopes, the
``kernel.show`` dict shape (which has no SDK dataclass), and the error
envelope live here — no second copy of SDK DTOs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...sdk.services import ContinuationResult
from ..storage.store import SessionRecord


class CoreHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _capability_list(value: object) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError("must be a list of non-empty strings")
    for item in items:
        if not isinstance(item, str) or not item or "\0" in item or len(item) > 128:
            raise ValueError("must be a list of non-empty strings")
    return items


class CreateSessionRequest(CoreHttpModel):
    session_id: str | None = Field(default=None, max_length=128)


class RunRequest(CoreHttpModel):
    prompt: str = Field(min_length=1, max_length=65536)
    session_id: str | None = Field(default=None, max_length=128)
    max_steps: int = Field(default=32, ge=1, le=1024)


class ResumeRequest(CoreHttpModel):
    approval_request_id: str | None = None
    decision: Literal["allow", "deny"] | None = None
    max_steps: int = Field(default=32, ge=1, le=1024)


class InvokeRequest(CoreHttpModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: The capability revision the client actually displayed. Required: a client
    #: must never let the core fall back to "whatever is current now".
    expected_revision: str = Field(min_length=1, max_length=128)
    client_capabilities: list[str] = Field(default_factory=list)

    _check_capabilities = field_validator("client_capabilities", mode="before")(_capability_list)


class ConfirmRequest(CoreHttpModel):
    request_id: str = Field(min_length=1, max_length=128)
    #: The revision of the catalog the confirmation was displayed under.
    expected_revision: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny"]
    client_capabilities: list[str] = Field(default_factory=list)

    _check_capabilities = field_validator("client_capabilities", mode="before")(_capability_list)


class DrainRequest(CoreHttpModel):
    limit: int = Field(default=10, ge=1, le=100)


class DrainControlRequest(CoreHttpModel):
    """Supervisor drain order: fence admission, settle admitted work, optionally pause runs."""

    timeout_seconds: float = Field(default=30.0, ge=0.0, le=600.0)
    require_quiescent: bool = False
    pause_at_batch_boundary: bool = False
    pause_intent: str = Field(default="", max_length=128)


class AdoptControlRequest(CoreHttpModel):
    """Adoption attribution: the acting session that owns the transition, when one exists."""

    session_id: str | None = Field(default=None, max_length=128)


class HealthBody(CoreHttpModel):
    """Core health and identity: the stable surface clients and the supervisor poll."""

    status: str
    fenced: bool
    stage: str
    pid: int
    workspace: str
    revision_id: str
    launch_protocol: int
    core_version: str
    image_digest: str
    web_bundle: str | None
    supervisor_pid: int
    generation: int
    active_upgrade: dict[str, Any] | None


class SessionsBody(CoreHttpModel):
    sessions: list[SessionRecord]


class ContinuationsBody(CoreHttpModel):
    continuations: list[ContinuationResult]


class CheckpointBody(CoreHttpModel):
    kind: str
    payload: dict[str, Any]


class StoredEventBody(CoreHttpModel):
    sequence: int
    type: str
    run_id: str
    revision_id: str
    source: str
    actor: str
    payload: dict[str, Any]


class ShowBody(CoreHttpModel):
    session_id: str
    status: str
    run_id: str | None
    revision_id: str
    turn: int
    last_event_sequence: int
    checkpoint: CheckpointBody | None
    events: list[StoredEventBody] | None = None


class ErrorBody(CoreHttpModel):
    error: str
    code: str | None = None
