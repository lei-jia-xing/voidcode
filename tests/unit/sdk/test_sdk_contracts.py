from __future__ import annotations

import pytest

from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.provider import ProviderMessage
from voidcode.sdk.tools import ToolCall, ToolDefinition, ToolEffect


def test_call_and_reasoning_snapshots_do_not_change_with_their_inputs() -> None:
    arguments = {"path": "file", "options": {"ranges": [1, 2]}}
    metadata = {"reasoning": {"signature": ["authentic"]}}
    call = ToolCall("read", arguments, "call-1")
    message = ProviderMessage("assistant", None, tool_calls=(call,), metadata=metadata)
    arguments["options"]["ranges"].append(3)
    metadata["reasoning"]["signature"].clear()
    assert json_wire_object(call.arguments) == {"path": "file", "options": {"ranges": [1, 2]}}
    assert json_wire_object(message.metadata) == {"reasoning": {"signature": ["authentic"]}}
    for invalid in (float("nan"), float("inf"), object()):
        with pytest.raises(ValueError):
            own_json_object({"value": invalid})


@pytest.mark.parametrize(
    ("effects", "replay"),
    [
        (frozenset({ToolEffect.READ}), "safe"),
        (frozenset(), "never"),
        (frozenset({ToolEffect.READ, ToolEffect.WRITE}), "never"),
        (frozenset({ToolEffect.READ, ToolEffect.NETWORK}), "never"),
        (frozenset({ToolEffect.EXECUTE}), "never"),
    ],
)
def test_implicit_replay_requires_unambiguously_read_only_effects(effects: frozenset[ToolEffect], replay: str) -> None:
    # The name and arguments cannot turn an executable tool into a safe read.
    definition = ToolDefinition("ast_grep", "search", effects=effects)
    assert definition.replay_policy_for({"mode": "search"}) == replay
