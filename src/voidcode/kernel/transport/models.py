"""HTTP-owned wire models for the kernel transport.

Request bodies validate at ingress (extra=forbid). Responses reuse the SDK and
storage dataclasses directly as ``response_model`` documentation; handlers
return their own ``Response`` built from ``as_payload()``, so FastAPI never
re-serializes ``MappingProxy`` payloads. Only list envelopes, the
``kernel.show`` dict shape (which has no SDK dataclass), and the error
envelope live here — no second copy of SDK DTOs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...sdk.commands import CommandDescriptor
from ...sdk.services import ContinuationResult
from ...sdk.views import PluginViewDescriptor
from ..storage.store import SessionRecord


class KernelHttpModel(BaseModel):
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


class CreateSessionRequest(KernelHttpModel):
    session_id: str | None = Field(default=None, max_length=128)


class RunRequest(KernelHttpModel):
    prompt: str = Field(min_length=1, max_length=65536)
    session_id: str | None = Field(default=None, max_length=128)
    max_steps: int = Field(default=32, ge=1, le=1024)


class ResumeRequest(KernelHttpModel):
    approval_request_id: str | None = None
    decision: Literal["allow", "deny"] | None = None
    max_steps: int = Field(default=32, ge=1, le=1024)


class InvokeRequest(KernelHttpModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    client_capabilities: list[str] = Field(default_factory=list)

    _check_capabilities = field_validator("client_capabilities", mode="before")(_capability_list)


class ConfirmRequest(KernelHttpModel):
    request_id: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny"]
    client_capabilities: list[str] = Field(default_factory=list)

    _check_capabilities = field_validator("client_capabilities", mode="before")(_capability_list)


class DrainRequest(KernelHttpModel):
    limit: int = Field(default=10, ge=1, le=100)


class SessionsBody(KernelHttpModel):
    sessions: list[SessionRecord]


class CommandsBody(KernelHttpModel):
    commands: list[CommandDescriptor]


class ViewsBody(KernelHttpModel):
    views: list[PluginViewDescriptor]


class ContinuationsBody(KernelHttpModel):
    continuations: list[ContinuationResult]


class CheckpointBody(KernelHttpModel):
    kind: str
    payload: dict[str, Any]


class StoredEventBody(KernelHttpModel):
    sequence: int
    type: str
    run_id: str
    revision_id: str
    source: str
    actor: str
    payload: dict[str, Any]


class ShowBody(KernelHttpModel):
    session_id: str
    status: str
    run_id: str | None
    revision_id: str
    turn: int
    last_event_sequence: int
    checkpoint: CheckpointBody | None
    events: list[StoredEventBody] | None = None


class ErrorBody(KernelHttpModel):
    error: str
    code: str | None = None
