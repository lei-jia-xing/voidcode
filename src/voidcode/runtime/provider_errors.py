from __future__ import annotations


class SingleAgentProviderError(ValueError):
    """Base runtime-classified provider error."""


class SingleAgentContextLimitError(SingleAgentProviderError):
    """Provider failure caused by context window exhaustion."""


def format_invalid_provider_config_error(field_path: str, reason: str) -> str:
    return f"invalid provider config: {field_path} {reason}"


def format_fallback_exhausted_error(*, provider_name: str, model_name: str, attempt: int) -> str:
    return (
        "provider fallback exhausted after "
        f"{provider_name}/{model_name} failed at attempt {attempt}"
    )


def classify_provider_error(exc: Exception) -> SingleAgentProviderError | None:
    message = str(exc).lower()
    context_limit_markers = (
        "context window",
        "context limit",
        "maximum context",
        "maximum context length",
        "token limit",
    )
    if any(marker in message for marker in context_limit_markers):
        return SingleAgentContextLimitError(str(exc))
    return None
