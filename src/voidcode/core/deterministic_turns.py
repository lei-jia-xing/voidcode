from __future__ import annotations

from ..command.resolver import resolve_tool_instruction
from ..tools.contracts import ToolResult
from .transcript import ToolResultView, tool_result_output
from .turns import LoopStepFact, ModelTurnFact, ResponseReadyFact, TurnPlan, TurnRequest, TurnSession


class DeterministicTurnProducer:
    """Adapt existing command instructions to the same complete turn contract."""

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResult | ToolResultView, ...],
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
            return TurnPlan(
                facts=(*facts, ResponseReadyFact(request.prompt[:200])),
                output=request.prompt,
                is_finished=True,
            )
        commands = [line.strip() for line in request.prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")
        step_index = sum(result.source != "replayed_conversation" for result in tool_results)
        if step_index < len(commands):
            call = resolve_tool_instruction(commands[step_index], request.available_tools, unavailable_message_suffix="turn execution")
            return TurnPlan(facts=facts, tool_calls=(call,))
        output = tool_result_output(tool_results[-1]) or ""
        return TurnPlan(
            facts=(
                *facts,
                LoopStepFact(request.run_step + 1, "finalize"),
                ResponseReadyFact(output),
            ),
            output=output,
            is_finished=True,
        )
