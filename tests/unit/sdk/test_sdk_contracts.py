from __future__ import annotations

import pytest

from voidcode.sdk.json_values import json_wire_object, own_json_object
from voidcode.sdk.provider import ProviderMessage
from voidcode.sdk.services import BudgetExhaustion, ChildSendReceipt
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
    assert definition.effective_replay_policy == replay


def _budget_payload() -> dict[str, object]:
    return {
        "dimension": "provider_turns",
        "source": "max_steps",
        "limit": 10,
        "consumed": 10,
        "remaining": 0,
        "continue_with": "resume_with_max_steps",
    }


def test_child_send_receipt_preserves_parked_delivery_in_its_payload() -> None:
    receipt = ChildSendReceipt(
        child_session_id="child-1",
        delivery="parked",
        child_status="interrupted",
        message_id="message-1",
    )
    assert receipt.as_payload() == {
        "child_session_id": "child-1",
        "delivery": "parked",
        "child_status": "interrupted",
        "message_id": "message-1",
    }


def test_budget_exhaustion_round_trips_and_absent_stays_absent() -> None:
    assert BudgetExhaustion.from_payload(None) is None
    parsed = BudgetExhaustion.from_payload(_budget_payload())
    assert parsed is not None
    assert parsed.as_payload() == _budget_payload()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "dimension": "owner_tokens",
            "source": "max_input_tokens",
            "limit": 120,
            "consumed": 130,
            "remaining": 0,
            "continue_with": "raise_owner_budget",
        },
        {
            "dimension": "owner_cost_usd",
            "source": "max_cost_usd",
            "limit": 1.25,
            "consumed": 2.5,
            "remaining": 0.0,
            "continue_with": "raise_owner_budget",
        },
        {
            "dimension": "owner_elapsed_seconds",
            "source": "max_elapsed_seconds",
            "limit": 60.0,
            "consumed": 61.5,
            "remaining": 0.0,
            "continue_with": "raise_owner_budget",
        },
    ],
)
def test_budget_exhaustion_owner_forms_round_trip(payload: dict[str, object]) -> None:
    parsed = BudgetExhaustion.from_payload(payload)
    assert parsed is not None
    assert parsed.as_payload() == payload


def test_budget_exhaustion_keeps_token_counters_integral() -> None:
    with pytest.raises(ValueError, match="integers for token and turn caps"):
        BudgetExhaustion.from_payload(
            {
                "dimension": "owner_tokens",
                "source": "max_total_tokens",
                "limit": 10.0,
                "consumed": 10.0,
                "remaining": 0.0,
                "continue_with": "raise_owner_budget",
            }
        )


def test_budget_exhaustion_rejects_anything_but_the_six_typed_fields() -> None:
    base = _budget_payload()
    bad: list[object] = ["nope", 42, []]
    missing = dict(base)
    del missing["remaining"]
    bad.append(missing)
    bad.append({**base, "extra": 1})
    bad.append({**base, "consumed": True})
    bad.append({**base, "limit": "10"})
    bad.append({**base, "remaining": 1.0})
    bad.append({**base, "consumed": -1})
    bad.append({**base, "source": "root_action_budget"})
    bad.append({**base, "dimension": "turns"})
    for value in bad:
        with pytest.raises(ValueError):
            BudgetExhaustion.from_payload(value)
