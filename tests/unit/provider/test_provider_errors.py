from __future__ import annotations

from voidcode.provider.errors import (
    SingleAgentContextLimitError,
    classify_provider_error,
    format_fallback_exhausted_error,
    format_invalid_provider_config_error,
    parse_provider_api_error,
    parse_provider_stream_error,
    provider_execution_error_from_api_payload,
    provider_execution_error_from_stream_payload,
)


def test_classify_provider_error_returns_context_limit_error_for_limit_messages() -> None:
    classified = classify_provider_error(ValueError("context window exceeded for provider"))

    assert isinstance(classified, SingleAgentContextLimitError)
    assert str(classified) == "context window exceeded for provider"


def test_classify_provider_error_returns_none_for_unrelated_errors() -> None:
    assert classify_provider_error(ValueError("network timeout")) is None


def test_format_invalid_provider_config_error_includes_field_and_reason() -> None:
    assert format_invalid_provider_config_error(
        "provider_fallback.preferred_model",
        "must match model when both are configured",
    ) == (
        "invalid provider config: provider_fallback.preferred_model "
        "must match model when both are configured"
    )


def test_format_fallback_exhausted_error_includes_provider_model_and_attempt() -> None:
    assert (
        format_fallback_exhausted_error(
            provider_name="opencode",
            model_name="gpt-5.4",
            attempt=2,
        )
        == "provider fallback exhausted after opencode/gpt-5.4 failed at attempt 2"
    )


def test_parse_provider_api_error_maps_429_to_rate_limit() -> None:
    parsed = parse_provider_api_error(
        {
            "status_code": 429,
            "error": {"message": "Too many requests", "code": "rate_limit_exceeded"},
        }
    )

    assert parsed.kind == "rate_limit"
    assert parsed.message == "Too many requests"
    assert parsed.details["source"] == "api"
    assert parsed.retryable is True
    assert parsed.fallback_allowed is True


def test_parse_provider_api_error_maps_401_to_missing_auth() -> None:
    parsed = parse_provider_api_error({"status_code": 401, "message": "Unauthorized"})

    assert parsed.kind == "missing_auth"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is True
    assert "API key" in parsed.guidance


def test_parse_provider_api_error_keeps_403_model_code_as_invalid_model() -> None:
    parsed = parse_provider_api_error(
        {
            "status_code": 403,
            "error": {
                "code": "model_not_found",
                "message": "model not found or no model access",
            },
        }
    )

    assert parsed.kind == "invalid_model"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is True


def test_parse_provider_api_error_maps_context_overflow_patterns() -> None:
    parsed = parse_provider_api_error(
        {
            "status_code": 400,
            "message": "Input exceeds context window of this model",
        }
    )

    assert parsed.kind == "context_limit"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is False


def test_parse_provider_api_error_maps_unsupported_feature() -> None:
    parsed = parse_provider_api_error(
        {"status_code": 400, "message": "Streaming is not supported for this model"}
    )

    assert parsed.kind == "unsupported_feature"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is True


def test_parse_provider_api_error_checks_unsupported_feature_before_403_fallback() -> None:
    parsed = parse_provider_api_error(
        {"status_code": 403, "message": "Streaming is not supported for this model"}
    )

    assert parsed.kind == "unsupported_feature"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is True
    assert "unsupported provider feature" in parsed.guidance


def test_parse_provider_api_error_keeps_explicit_auth_code_before_feature_marker() -> None:
    parsed = parse_provider_api_error(
        {
            "status_code": 403,
            "error": {
                "code": "invalid_api_key",
                "message": "invalid api key; streaming is not supported",
            },
        }
    )

    assert parsed.kind == "missing_auth"


def test_parse_provider_api_error_checks_tool_shape_before_403_fallback() -> None:
    parsed = parse_provider_api_error(
        {"status_code": 403, "message": "stream tool payload is malformed"}
    )

    assert parsed.kind == "stream_tool_feedback_shape"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is True


def test_parse_provider_stream_error_maps_context_length_exceeded_code() -> None:
    parsed = parse_provider_stream_error(
        {
            "type": "error",
            "error": {
                "code": "context_length_exceeded",
                "message": "input token count exceeds the maximum",
            },
        }
    )

    assert parsed.kind == "context_limit"
    assert parsed.details["source"] == "stream"
    assert parsed.retryable is False
    assert parsed.fallback_allowed is False


def test_provider_execution_error_from_api_payload_preserves_details() -> None:
    exc = provider_execution_error_from_api_payload(
        provider_name="openai",
        model_name="gpt-5.4",
        payload={"status_code": 503, "message": "upstream unavailable"},
    )

    assert exc.kind == "transient_failure"
    assert exc.retryable is True
    assert exc.details is not None
    assert exc.details["status_code"] == 503
    assert exc.details["source"] == "api"


def test_provider_execution_error_redacts_secret_details() -> None:
    exc = provider_execution_error_from_api_payload(
        provider_name="openai",
        model_name="gpt-4o",
        payload={
            "status_code": 401,
            "message": "invalid api key",
            "api_key": "sk-secret",
            "headers": {
                "Authorization": "Bearer sk-secret",
                "x-api-key": "secret-header",
                "cookie": "session=secret-cookie",
            },
            "url": "https://example.invalid?api_key=secret-query-token",
            "debug": "raw token=secret-value",
        },
    )

    assert exc.details is not None
    assert exc.kind == "missing_auth"
    assert exc.details["api_key"] == "<redacted>"
    assert exc.details["headers"] == {
        "Authorization": "<redacted>",
        "x-api-key": "<redacted>",
        "cookie": "<redacted>",
    }
    assert exc.details["url"] == "https://example.invalid?api_key=<redacted>"
    assert exc.details["debug"] == "raw token=<redacted>"


def test_provider_execution_error_from_stream_payload_preserves_details() -> None:
    exc = provider_execution_error_from_stream_payload(
        provider_name="openai",
        model_name="gpt-5.4",
        payload={"status_code": 403, "message": "forbidden"},
    )

    assert exc.kind == "missing_auth"
    assert exc.retryable is False
    assert exc.details is not None
    assert exc.details["status_code"] == 403
    assert exc.details["source"] == "stream"
