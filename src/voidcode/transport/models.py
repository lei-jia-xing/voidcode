"""Strict RPC parameter models and transport-owned result envelopes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from voidcode.core.artifacts import MAX_PAGE_BYTES
from voidcode.sdk.client import SessionFrameWire, SessionRow
from voidcode.sdk.services import ContinuationResult
from voidcode.sdk.transcript import TranscriptEntryWire

RPC_SUBPROTOCOL = "voidcode.rpc.v1"
RPC_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
RPC_MAX_PENDING = 32
RPC_MAX_SUBSCRIPTIONS = 16
RPC_EVENT_WINDOW = 64
RPC_QUEUE_MESSAGES = 64
RPC_QUEUE_BYTES = 8 * 1024 * 1024


class RpcModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RpcRequest(RpcModel):
    jsonrpc: Literal["2.0"]
    method: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] | list[Any] = Field(default_factory=dict)
    id: str | int | None = None


class RpcError(RpcModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class RpcSuccess(RpcModel):
    jsonrpc: Literal["2.0"]
    id: str | int | None
    result: Any


class RpcFailure(RpcModel):
    jsonrpc: Literal["2.0"]
    id: str | int | None
    error: RpcError


class RpcNotification(RpcModel):
    jsonrpc: Literal["2.0"]
    method: str
    params: dict[str, Any]


#: The one session-id rule every ingress path shares: a non-empty token without
#: ``/`` or NUL, bounded so it stays a map key and a log field, never a blob.
SESSION_ID_PATTERN = r"^[^/\x00]+$"
SESSION_ID_MAX_LENGTH = 128


class EmptyParams(RpcModel):
    pass


class AuthenticateParams(RpcModel):
    token: str | None = Field(default=None, max_length=4096)


class SessionParams(RpcModel):
    session_id: str = Field(min_length=1, max_length=SESSION_ID_MAX_LENGTH, pattern=SESSION_ID_PATTERN)


class ArtifactReadParams(RpcModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=65536, ge=1, le=MAX_PAGE_BYTES)


class ListSessionsParams(RpcModel):
    limit: int = Field(default=100, ge=1, le=1000)


class CreateSessionRequest(RpcModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=SESSION_ID_MAX_LENGTH, pattern=SESSION_ID_PATTERN)


class ShowParams(SessionParams):
    events: bool = False


class ImageAttachmentPayload(RpcModel):
    media_type: str = Field(min_length=1, max_length=128)
    data: str = Field(min_length=1)
    artifact_id: str | None = None


class RunRequest(SessionParams):
    prompt: str = Field(min_length=1, max_length=65536)
    max_steps: int | None = Field(default=None, ge=1)
    root_action_budget: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    images: list[ImageAttachmentPayload] = Field(default_factory=list)
    interaction: Literal["interactive", "unattended"] | None = None


class ResumeRequest(SessionParams):
    approval_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    decision: Literal["allow", "deny"] | None = None
    max_steps: int | None = Field(default=None, ge=1)
    extend_root_actions: int | None = Field(default=None, gt=0, le=9_223_372_036_854_775_807)
    root_action_grant_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^\x00]+$")
    interaction: Literal["interactive", "unattended"] | None = None

    @model_validator(mode="after")
    def paired_decision_and_grant(self) -> ResumeRequest:
        if (self.approval_request_id is None) != (self.decision is None):
            raise ValueError("approval_request_id and decision travel together")
        if (self.extend_root_actions is None) != (self.root_action_grant_id is None):
            raise ValueError("extend_root_actions and root_action_grant_id travel together")
        return self


class CommandInputRequest(SessionParams):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^/\x00]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_revision: str = Field(min_length=1, max_length=128)


class InvokeRequest(SessionParams):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[^/\x00]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_revision: str = Field(min_length=1, max_length=128)


class ConfirmRequest(SessionParams):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny"]


class AskAnswerRequest(SessionParams):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: str = Field(min_length=1, max_length=128)
    values: dict[str, Any]


class AskCancelRequest(SessionParams):
    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: str = Field(min_length=1, max_length=128)


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
