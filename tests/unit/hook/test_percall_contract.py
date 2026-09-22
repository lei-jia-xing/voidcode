from __future__ import annotations

from copy import deepcopy

from voidcode.hook.percall import (
    PerCallChain,
    PerCallHandlerBinding,
    PerCallMessage,
    PerCallRewriteDecision,
    percall_cache_prefix,
    percall_messages_sha256,
    percall_persistent_messages,
)


def _append(tag: str):  # type: ignore[no-untyped-def]
    def handler(messages: tuple[PerCallMessage, ...]) -> PerCallRewriteDecision:
        seen = [m.content for m in messages]
        return PerCallRewriteDecision(
            action="rewrite",
            messages=(*messages, PerCallMessage(role="ctx", content=f"{tag}:{'+'.join(seen)}")),
        )

    return handler


def test_chain_order_is_registration_order() -> None:
    chain = PerCallChain(
        bindings=(
            PerCallHandlerBinding(name="first", handler=_append("a")),
            PerCallHandlerBinding(name="second", handler=_append("b")),
        )
    )
    outcome = chain.apply(messages=(PerCallMessage(role="user", content="hi"),))
    assert outcome.handler_names == ("first", "second")
    assert [m.content for m in outcome.messages] == ["hi", "a:hi", "b:hi+a:hi"]


def test_clone_isolation_input_history_untouched() -> None:
    chain = PerCallChain(bindings=(PerCallHandlerBinding(name="only", handler=_append("x")),))
    history = (PerCallMessage(role="user", content="orig"),)
    snapshot = deepcopy(history)
    outcome = chain.apply(messages=history)
    assert history == snapshot
    assert len(outcome.messages) == 2


def test_marker_excluded_from_persistence_and_cache() -> None:
    persistent = PerCallMessage(role="user", content="keep")
    ephemeral = PerCallMessage(role="ctx", content="tmp", per_call=True)
    messages = (persistent, ephemeral)
    assert percall_persistent_messages(messages) == (persistent,)
    assert percall_cache_prefix(messages) == percall_cache_prefix((persistent,))
    assert percall_messages_sha256(messages) == percall_messages_sha256((persistent,))
    assert "tmp" not in percall_cache_prefix(messages)
