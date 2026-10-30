"""Strict RPC parameter models and transport-owned result envelopes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from voidcode.sdk.client import SessionFrameWire, SessionRow
from voidcode.sdk.rpc import RpcModel
from voidcode.sdk.services import ContinuationResult
from voidcode.sdk.transcript import TranscriptEntryWire


class EmptyParams(RpcModel):
    pass


class AuthenticateParams(RpcModel):
    token: str | None = Field(default=None, max_length=4096)


class SessionParams(RpcModel):
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[^/\x00]+$")


class ListSessionsParams(RpcModel):
    limit: int = Field(default=100, ge=1, le=1000)


class CreateSessionRequest(RpcModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^/\x00]+$")


class ShowParams(SessionParams):
    events: bool = False


class RunRequest(SessionParams):
    prompt: str = Field(min_length=1, max_length=65536)
    max_steps: int = Field(default=32, ge=1, le=1024)


class ResumeRequest(SessionParams):
    approval_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    decision: Literal["allow", "deny"] | None = None
    max_steps: int = Field(default=32, ge=1, le=1024)

    @model_validator(mode="after")
    def paired_decision(self) -> ResumeRequest:
        if (self.approval_request_id is None) != (self.decision is None):
            raise ValueError("approval_request_id and decision travel together")
        return self


class CommandInputRequest(SessionParams):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^/\x00]+$")
    expected_revision: str = Field(min_length=1, max_length=128)


class InvokeRequest(SessionParams):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^/\x00]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_revision: str = Field(min_length=1, max_length=128)


class ConfirmRequest(SessionParams):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny"]


class DrainRequest(SessionParams):
    limit: int = Field(default=10, ge=1, le=100)


class UsageParams(SessionParams):
    since_sequence: int = Field(default=0, ge=0)
    until_sequence: int | None = Field(default=None, ge=0)


class TranscriptParams(SessionParams):
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class ContextParams(SessionParams):
    limit: int = Field(default=100, ge=1, le=1000)


class SubscribeParams(SessionParams):
    subscription_id: str = Field(min_length=1, max_length=128)
    after_sequence: int = Field(default=0, ge=0)
    follow: bool = True


class UnsubscribeParams(RpcModel):
    subscription_id: str = Field(min_length=1, max_length=128)


class SubscriptionReceipt(RpcModel):
    subscription_id: str


class UnsubscribeReceipt(RpcModel):
    removed: bool


class AuthenticateReceipt(RpcModel):
    protocol_version: Literal[1] = 1


class SessionsBody(RpcModel):
    sessions: list[SessionRow]


class ContinuationsBody(RpcModel):
    continuations: list[ContinuationResult]


class CheckpointBody(RpcModel):
    kind: str
    payload: dict[str, Any]


class StoredEventBody(RpcModel):
    sequence: int
    type: str
    run_id: str
    revision_id: str
    source: str
    actor: str
    payload: dict[str, Any]
    transcript: TranscriptEntryWire | None = None


class ShowBody(RpcModel):
    session_id: str
    status: str
    run_id: str | None
    revision_id: str
    turn: int
    last_event_sequence: int
    checkpoint: CheckpointBody | None
    events: list[StoredEventBody] | None = None


class SessionFrameParams(RpcModel):
    subscription_id: str
    frame: SessionFrameWire


class SubscriptionClosedParams(RpcModel):
    subscription_id: str


class AckParams(RpcModel):
    subscription_id: str = Field(min_length=1, max_length=128)
    through_sequence: int = Field(ge=0)


class AckReceipt(RpcModel):
    through_sequence: int
