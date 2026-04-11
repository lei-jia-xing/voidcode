from __future__ import annotations

from voidcode.runtime.provider_errors import SingleAgentContextLimitError, classify_provider_error


def test_classify_provider_error_returns_context_limit_error_for_limit_messages() -> None:
    classified = classify_provider_error(ValueError("context window exceeded for provider"))

    assert isinstance(classified, SingleAgentContextLimitError)
    assert str(classified) == "context window exceeded for provider"


def test_classify_provider_error_returns_none_for_unrelated_errors() -> None:
    assert classify_provider_error(ValueError("network timeout")) is None
