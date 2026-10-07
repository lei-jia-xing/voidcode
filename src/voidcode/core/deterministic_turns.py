from __future__ import annotations

from ..command.resolver import resolve_tool_instruction
from .transcript import ToolResultView, tool_result_output
from .turns import FinalTurn, LoopStepFact, ModelTurnFact, ResponseReadyFact, ToolTurn, TurnPlan, TurnRequest, TurnSession


class DeterministicTurnProducer:
    """Adapt existing command instructions to the same complete turn contract."""

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> TurnPlan:
        _ = session
        if request.run_step < 1:
            raise ValueError("run_step must be a positive integer")
        facts = (
            LoopStepFact(request.run_step, "plan"),
            ModelTurnFact(request.run_step, "deterministic", request.prompt),
        )
        if request.run_step == 1 and isinstance(request.metadata.get("command"), dict):
            return FinalTurn(
                facts=(*facts, ResponseReadyFact(request.prompt[:200])),
                output=request.prompt,
            )
        commands = [line.strip() for line in request.prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")
        step_index = request.run_step - 1
        if step_index < len(commands):
            call = resolve_tool_instruction(commands[step_index], request.available_tools, unavailable_message_suffix="turn execution")
            return ToolTurn(calls=(call,), facts=facts)
        output = tool_result_output(tool_results[-1]) or ""
        return FinalTurn(
            facts=(
                *facts,
                LoopStepFact(request.run_step + 1, "finalize"),
                ResponseReadyFact(output),
            ),
            output=output,
        )
