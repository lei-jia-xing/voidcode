from __future__ import annotations

from voidcode.core.turns import ModelTurnFact, TurnRequest


def model_turn_fact(request: TurnRequest) -> ModelTurnFact:
    return ModelTurnFact(request.run_step, "deterministic", request.prompt)
