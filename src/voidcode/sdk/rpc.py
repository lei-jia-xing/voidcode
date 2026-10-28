"""JSON-RPC 2.0 message contracts shared by remote clients and transport."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
