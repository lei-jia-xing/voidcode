"""Tests for SDK codec strict boundaries and real object roundtrips."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from voidcode.sdk.codec import (
    decode_message,
    decode_provider_error,
    decode_provider_result,
    decode_tool_call,
    decode_tool_result,
    encode_provider_error,
    encode_provider_result,
    encode_tool_result,
)
from voidcode.sdk.provider import (
    ProviderExecutionError,
    ProviderTokenUsage,
    ProviderTurnResult,
)
from voidcode.sdk.tools import (
    AttachmentOutput,
    ConfirmedStop,
    OpaqueToolBody,
    OutputBounds,
    OutputReference,
    TextOutput,
    ToolDiagnostics,
    ToolFailure,
    ToolSuccess,
)


def test_decode_tool_call_strict_ingress_boundaries() -> None:
    # Missing tool_name
    with pytest.raises(ValueError):
        decode_tool_call({"arguments": {}})

    # Non-string tool_name
    with pytest.raises(ValueError):
        decode_tool_call({"tool_name": 123, "arguments": {}})

    # Empty tool_name
    with pytest.raises(ValueError):
        decode_tool_call({"tool_name": "", "arguments": {}})

    # Invalid arguments type (must not silently replace with {})
    with pytest.raises(ValueError):
        decode_tool_call({"tool_name": "test", "arguments": "invalid"})

    with pytest.raises(ValueError):
        decode_tool_call({"tool_name": "test", "arguments": [1, 2, 3]})

    # Valid call
    call = decode_tool_call({"tool_name": "read_file", "arguments": {"path": "a.txt"}, "tool_call_id": "c1"})
    assert call.tool_name == "read_file"
    assert call.arguments == {"path": "a.txt"}
    assert call.tool_call_id == "c1"


def test_tool_result_with_actual_sdk_fields_roundtrip() -> None:
    # ToolSuccess with real fields
    body = OpaqueToolBody(structured_content=MappingProxyType({"lines": 10}))
    bounds = OutputBounds(truncated=True, partial=False, reference=OutputReference(uri="void://art/1"), source="fs")
    success = ToolSuccess(
        tool_name="read",
        output=AttachmentOutput(mime="text/plain", data_uri="data:text/plain;base64,aGVsbG8=", bounds=bounds),
        body=body,
    )
    enc_success = encode_tool_result(success)
    assert enc_success["status"] == "ok"
    dec_success = decode_tool_result(enc_success)
    assert isinstance(dec_success, ToolSuccess)
    assert isinstance(dec_success.output, AttachmentOutput)
    assert dec_success.output.data_uri == "data:text/plain;base64,aGVsbG8="
    assert dec_success.output.bounds.truncated is True
    assert dec_success.output.bounds.reference is not None
    assert dec_success.output.bounds.reference.uri == "void://art/1"
    assert dec_success.body is not None
    assert dec_success.body.as_payload() == {"lines": 10}

    # ToolFailure with real error and diagnostics
    diag = ToolDiagnostics(kind="syntax", summary="syntax error", details={"line": 5}, guidance="check syntax")
    failure = ToolFailure(
        tool_name="read",
        error="File not found",
        output=TextOutput(text="Error occurred"),
        diagnostics=diag,
        execution=ConfirmedStop(cancellation_signalled=True),
        timeout_seconds=30,
    )
    enc_failure = encode_tool_result(failure)
    assert enc_failure["status"] == "error"
    assert enc_failure["error"] == "File not found"
    dec_failure = decode_tool_result(enc_failure)
    assert isinstance(dec_failure, ToolFailure)
    assert dec_failure.error == "File not found"
    assert dec_failure.diagnostics is not None
    assert dec_failure.diagnostics.kind == "syntax"
    assert dec_failure.diagnostics.summary == "syntax error"
    assert dec_failure.execution is not None
    assert dec_failure.execution.cancellation_signalled is True
    assert dec_failure.timeout_seconds == 30


def test_provider_wire_types_with_actual_sdk_fields() -> None:
    # ProviderTokenUsage real fields: cache_write_tokens, uncached_input_tokens, cost_usd
    usage = ProviderTokenUsage(
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=40,
        cache_write_tokens=60,
        uncached_input_tokens=60,
        cost_usd=0.005,
    )
    result = ProviderTurnResult(
        output="Hello",
        usage=usage,
        done_reason="stop",
        finish_reason_reported=True,
    )
    enc = encode_provider_result(result)
    dec = decode_provider_result(enc)
    assert dec.output == "Hello"
    assert dec.usage is not None
    assert dec.usage.cache_write_tokens == 60
    assert dec.usage.uncached_input_tokens == 60
    assert dec.usage.cost_usd == 0.005
    assert dec.finish_reason_reported is True


def test_decode_message_strict_role_boundary() -> None:
    with pytest.raises(ValueError):
        decode_message({"role": "invalid_role", "content": "hello"})

    with pytest.raises(ValueError):
        decode_message({"role": "user", "content": "hi", "tool_calls": "not-a-list"})

    msg = decode_message({"role": "user", "content": "hi", "tool_calls": []})
    assert msg.role == "user"
    assert msg.content == "hi"


def test_provider_error_roundtrip() -> None:
    err = ProviderExecutionError(
        kind="rate_limit",
        provider_name="deepseek",
        model_name="deepseek-flash",
        message="Rate limit reached",
        retryable=True,
        retry_after=5.0,
    )
    enc = encode_provider_error(err)
    assert enc["kind"] == "rate_limit"
    assert enc["provider_name"] == "deepseek"
    dec = decode_provider_error(enc)
    assert isinstance(dec, ProviderExecutionError)
    assert dec.kind == "rate_limit"
    assert dec.provider_name == "deepseek"
    assert dec.model_name == "deepseek-flash"
    assert dec.message == "Rate limit reached"
    assert dec.retryable is True
    assert dec.retry_after == 5.0
