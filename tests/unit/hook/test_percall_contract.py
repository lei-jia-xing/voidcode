from __future__ import annotations

from voidcode.hook.percall import (
    PerCallMessage,
    percall_cache_prefix,
    percall_messages_sha256,
    percall_persistent_messages,
)


def test_marker_excluded_from_persistence_and_cache() -> None:
    persistent = PerCallMessage(role="user", content="keep")
    ephemeral = PerCallMessage(role="ctx", content="tmp", per_call=True)
    messages = (persistent, ephemeral)
    assert percall_persistent_messages(messages) == (persistent,)
    assert percall_cache_prefix(messages) == percall_cache_prefix((persistent,))
    assert percall_messages_sha256(messages) == percall_messages_sha256((persistent,))
    assert "tmp" not in percall_cache_prefix(messages)
