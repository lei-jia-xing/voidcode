from typing import Any

import pytest

from voidcode.core.turns import CallSeed
from voidcode.runtime.events import model_turn_batch_id
from voidcode.runtime.execution.turn_recovery import persisted_turn_batch, restored_turn_batch
from voidcode.tools.contracts import ToolCall


def _metadata() -> dict[str, Any]:
    batch = CallSeed(
        (ToolCall("read", {"path": "source.txt"}, "call-1"),),
        run_step=1,
        tool_batch_id=model_turn_batch_id("session", 1),
    )
    return {
        "runtime_state": {
            "run_id": "active-resume",
            "composition_turn_occurrence": 1,
            "composition_turn": {
                "session_id": "session",
                "run_id": "producer",
                "scope_id": "session:producer:1",
                "occurrence": 1,
                "completed": False,
                "invoked_call_ids": [],
            },
            "turn_batch": persisted_turn_batch(batch, session_id="session", run_id="producer", started_sequence=0),
        }
    }


def test_restored_batch_uses_original_producer_not_active_resume() -> None:
    batch, started = restored_turn_batch(_metadata(), session_id="session", tool_results=())
    assert started == 0
    assert batch.calls[0].tool_call_id == "call-1"


@pytest.mark.parametrize("run_id", [None, "", "   ", 1, "active-resume", "other-producer"])
def test_restored_batch_rejects_missing_or_mismatched_origin(run_id: object) -> None:
    metadata = _metadata()
    metadata["runtime_state"]["turn_batch"]["run_id"] = run_id
    with pytest.raises(ValueError, match="producer run identity|original composition turn"):
        restored_turn_batch(metadata, session_id="session", tool_results=())


@pytest.mark.parametrize(
    ("field", "value"),
    [("session_id", "other"), ("scope_id", "fake"), ("occurrence", 2), ("completed", None), ("invoked_call_ids", ["call-1", "call-1"])],
)
def test_restored_batch_requires_validated_producer_cursor(field: str, value: object) -> None:
    metadata = _metadata()
    metadata["runtime_state"]["composition_turn"][field] = value
    with pytest.raises(ValueError):
        restored_turn_batch(metadata, session_id="session", tool_results=())


@pytest.mark.parametrize("run_id", [None, "", "   ", 1])
def test_persisted_batch_requires_actual_producer_identity(run_id: Any) -> None:
    batch = CallSeed((ToolCall("read", {}, "call-1"),), run_step=1)
    with pytest.raises(ValueError, match="producer run identity"):
        persisted_turn_batch(batch, session_id="session", run_id=run_id, started_sequence=0)
