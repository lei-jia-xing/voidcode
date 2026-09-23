"""Per-call cache-prefix seam in assemble_provider_context."""

from __future__ import annotations

from voidcode.hook.percall import PerCallMessage, percall_cache_prefix
from voidcode.runtime.context.window import assemble_provider_context


def test_cache_prefix_hashes_bound_segments() -> None:
    assembled = assemble_provider_context(
        prompt="current question",
        tool_results=(),
        session_metadata={},
    )
    prefix = assembled.metadata["percall_cache_prefix"]
    assert isinstance(prefix, str) and prefix
    bound = tuple(PerCallMessage(role=segment.role, content=segment.content or "") for segment in assembled.segments)
    assert prefix == percall_cache_prefix(bound)
