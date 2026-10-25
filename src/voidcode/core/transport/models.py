"""HTTP-owned wire models for the core transport.

Request bodies validate at ingress (extra=forbid). Responses reuse the SDK and
storage dataclasses directly as ``response_model`` documentation — including the
``CommandCatalog``/``ViewsSnapshot`` envelopes, which are SDK-owned — and
handlers return their own ``Response`` built from ``as_payload()``, so FastAPI
never re-serializes ``MappingProxy`` payloads. Only list envelopes, the
``core.show`` dict shape (which has no SDK dataclass), and the error
envelope live here — no second copy of SDK DTOs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...sdk.services import ContinuationResult
from ..storage.store import SessionRecord


class CoreHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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
    model_config = ConfigDict(extra="ignore")
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: The capability revision the client actually displayed. Required: a client
    #: must never let the core fall back to "whatever is current now".
    expected_revision: str = Field(min_length=1, max_length=128)


class ConfirmRequest(CoreHttpModel):
    model_config = ConfigDict(extra="ignore")
    request_id: str = Field(min_length=1, max_length=128)
    #: The revision of the catalog the confirmation was displayed under.
    expected_revision: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny"]


class DrainRequest(CoreHttpModel):
    limit: int = Field(default=10, ge=1, le=100)


class HealthBody(CoreHttpModel):
    """Core health and identity: the stable surface clients and the supervisor poll."""

    status: str
    stage: str
    pid: int
    workspace: str
    revision_id: str
    launch_protocol: int
    core_version: str
    web_bundle: str | None
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
    transcript: dict[str, Any] | None = None


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
