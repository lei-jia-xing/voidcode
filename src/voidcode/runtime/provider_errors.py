from __future__ import annotations


class SingleAgentProviderError(ValueError):
    """Base runtime-classified provider error."""


class SingleAgentContextLimitError(SingleAgentProviderError):
    """Provider failure caused by context window exhaustion."""


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
