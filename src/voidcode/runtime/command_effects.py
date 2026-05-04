from __future__ import annotations

import json
from typing import Protocol, cast

from ..command import CommandResolution
from .contracts import RuntimeRequestError
from .session import SessionState
from .task import ContinuationLoopState, StoredContinuationLoopSummary

INTENSIVE_LOOP_MAX_ITERATIONS = 500


class RuntimeCommandEffectHost(Protocol):
    def start_continuation_loop(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        completion_promise: str = "DONE",
        max_iterations: int = 100,
        intensive: bool = False,
    ) -> ContinuationLoopState: ...

    def cancel_continuation_loop(self, loop_id: str) -> ContinuationLoopState: ...

    def list_continuation_loops(self) -> tuple[StoredContinuationLoopSummary, ...]: ...

    def _load_existing_session_if_present(self, *, session_id: str) -> object | None: ...


def apply_runtime_command_effects(
    *,
    host: RuntimeCommandEffectHost,
    resolution: CommandResolution,
    metadata: dict[str, object],
) -> tuple[str, dict[str, object]]:
    invocation = resolution.invocation
    command_name = invocation.name
    if command_name == "start-work":
        return hydrate_start_work_prompt(
            host=host,
            prompt=invocation.rendered_prompt,
            raw_arguments=invocation.raw_arguments,
            arguments=invocation.arguments,
            metadata=metadata,
        )
    if command_name not in {"continuation-loop", "intensive-loop", "cancel-continuation"}:
        return invocation.original_prompt, metadata
    raw_arguments = invocation.raw_arguments.strip()
    if command_name == "cancel-continuation":
        try:
            cancelled = _cancel_requested_continuation_loop(
                host=host,
                raw_loop_id=raw_arguments,
            )
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        loop_payload = continuation_loop_metadata(cancelled)
        metadata["continuation_loop"] = loop_payload
        prompt = "\n\n".join(
            (
                invocation.rendered_prompt,
                "Runtime continuation loop cancellation result:",
                json.dumps(loop_payload, sort_keys=True),
            )
        )
        return prompt, metadata

    intensive = command_name == "intensive-loop"
    try:
        loop = host.start_continuation_loop(
            prompt=raw_arguments or invocation.original_prompt,
            intensive=intensive,
            max_iterations=INTENSIVE_LOOP_MAX_ITERATIONS if intensive else 100,
        )
    except ValueError as exc:
        raise RuntimeRequestError(str(exc)) from exc
    loop_payload = continuation_loop_metadata(loop)
    metadata["continuation_loop"] = loop_payload
    prompt_parts = [invocation.rendered_prompt]
    if loop.intensive:
        prompt_parts.append(render_intensive_loop_prefix(loop))
    prompt_parts.extend(
        (
            "Runtime continuation loop state:",
            json.dumps(loop_payload, sort_keys=True),
        )
    )
    return "\n\n".join(prompt_parts), metadata


def hydrate_start_work_prompt(
    *,
    host: RuntimeCommandEffectHost,
    prompt: str,
    raw_arguments: str,
    arguments: tuple[str, ...],
    metadata: dict[str, object],
) -> tuple[str, dict[str, object]]:
    if not arguments:
        return prompt, metadata
    plan_session_id = arguments[0]
    plan_response = host._load_existing_session_if_present(session_id=plan_session_id)
    if plan_response is None:
        return prompt, metadata
    session = getattr(plan_response, "session", None)
    raw_metadata = getattr(session, "metadata", None)
    if not isinstance(raw_metadata, dict):
        return prompt, metadata
    raw_plan = raw_metadata.get("workflow_plan")
    if not isinstance(raw_plan, dict):
        return prompt, metadata
    plan = dict(cast(dict[str, object], raw_plan))
    hydrated_prompt = "\n\n".join(
        (
            prompt,
            "<workflow_plan_artifact>",
            f"source_session_id: {plan_session_id}",
            f"requested_plan_reference: {raw_arguments}",
            f"plan_goal: {plan.get('goal', '')}",
            "plan_handoff:",
            str(plan.get("handoff", "")),
            "</workflow_plan_artifact>",
        )
    )
    normalized = dict(metadata)
    normalized["workflow_plan"] = {
        "snapshot_version": 1,
        "source": "start-work",
        "source_session_id": plan_session_id,
        "plan": plan,
    }
    return hydrated_prompt, normalized


def session_with_command_artifacts(
    session: SessionState,
    *,
    output: str | None,
) -> SessionState:
    raw_command = session.metadata.get("command")
    if not isinstance(raw_command, dict):
        return session
    command = cast(dict[str, object], raw_command)
    if command.get("name") != "plan":
        return session
    workflow_preset = session.metadata.get("workflow_preset")
    if workflow_preset != "review":
        return session
    plan_output = output or ""
    raw_arguments = command.get("raw_arguments")
    workflow_plan = {
        "snapshot_version": 1,
        "source": "plan-command",
        "session_id": session.session.id,
        "command": dict(command),
        "goal": raw_arguments if isinstance(raw_arguments, str) else "",
        "handoff": plan_output,
        "status": "draft" if session.status != "completed" else "ready",
    }
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata={**session.metadata, "workflow_plan": workflow_plan},
    )


def continuation_loop_metadata(loop: ContinuationLoopState) -> dict[str, object]:
    return {
        "loop_id": loop.loop.id,
        "status": loop.status,
        "prompt": loop.prompt,
        "session_id": loop.session_id,
        "completion_promise": loop.completion_promise,
        "max_iterations": loop.max_iterations,
        "iteration": loop.iteration,
        "intensive": loop.intensive,
        "strategy": loop.strategy,
        "created_at": loop.created_at,
        "updated_at": loop.updated_at,
        "finished_at": loop.finished_at,
        "cancel_requested_at": loop.cancel_requested_at,
        "error": loop.error,
    }


def render_intensive_loop_prefix(loop: ContinuationLoopState) -> str:
    return "\n".join(
        (
            "intensive",
            "Runtime continuation mode: intensive.",
            f"Iteration budget: {loop.max_iterations}.",
            f"Completion promise: {loop.completion_promise}.",
            "Before declaring completion, verify the result with the strongest targeted "
            "checks available and explicitly account for unresolved risks.",
        )
    )


def _cancel_requested_continuation_loop(
    *,
    host: RuntimeCommandEffectHost,
    raw_loop_id: str,
) -> ContinuationLoopState:
    if raw_loop_id:
        return host.cancel_continuation_loop(raw_loop_id)
    active_loop = next(
        (loop for loop in host.list_continuation_loops() if loop.status == "active"),
        None,
    )
    if active_loop is None:
        raise ValueError("no active continuation loop to cancel")
    return host.cancel_continuation_loop(active_loop.loop.id)
