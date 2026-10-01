from __future__ import annotations

from ...core.tool_context import ToolContext
from ..contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult


class TaskTool:
    definition = ToolDefinition(
        name="task",
        description=(
            "Delegate work to a child runtime session. Always include prompt, "
            "run_in_background, load_skills, and subagent_type. Prefer run_in_background=true "
            "for delegated work that can run independently. Each background task emits a "
            "completion event to its parent; when launching several tasks for one deliverable, "
            "wait for all known tasks to finish before synthesizing the final answer."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Full delegated task prompt for the child session.",
                    "minLength": 1,
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Required. true starts delegated work in the background and returns "
                        "a task_id. false runs the child session synchronously. Prefer true "
                        "for independent delegated work."
                    ),
                },
                "load_skills": {
                    "type": "array",
                    "description": ("Required. Array of skill names to force-load in the child session. Pass [] when no extra skills are needed."),
                    "items": {
                        "type": "string",
                        "minLength": 1,
                    },
                },
                "subagent_type": {
                    "type": "string",
                    "description": ("Required. Explicit child preset: advisor, explore, researcher, worker, or product."),
                    "minLength": 1,
                },
                "description": {
                    "type": "string",
                    "description": "Optional short delegation description.",
                    "minLength": 1,
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional existing child session id to continue.",
                    "minLength": 1,
                },
                "command": {
                    "type": "string",
                    "description": "Optional originating command label for delegated work.",
                    "minLength": 1,
                },
                "parallel_group_id": {
                    "type": "string",
                    "description": "Optional shared id for parallel tasks serving one deliverable.",
                    "minLength": 1,
                },
                "parallel_group_size": {
                    "type": "integer",
                    "description": "Expected number of tasks in the parallel group.",
                    "minimum": 1,
                },
                "keep_alive": {
                    "type": "boolean",
                    "description": (
                        "Optional. true keeps the delegated child session alive across steer "
                        "turns: after each turn without a handoff the task parks as idle "
                        "(awaiting_steer) and the leader resumes it with task(operation=steer). Requires "
                        "run_in_background=true."
                    ),
                },
                "outputSchema": {
                    "type": "object",
                    "description": (
                        "Optional arbitrary JSON Schema (outputSchema) declaring the structured "
                        "shape of the child's yield data. The child's final data is "
                        "validated against this schema at task finalize and surfaced as "
                        "structured_output. Requires run_in_background=true."
                    ),
                },
                "schemaMode": {
                    "type": "string",
                    "enum": ["permissive", "strict"],
                    "description": (
                        "Optional validation strictness for outputSchema. permissive (default) "
                        "keeps the task completed with schema_validation.valid=false on "
                        "failure; strict fails the task with the validation error. Ignored "
                        "without outputSchema."
                    ),
                },
                "operation": {"type": "string", "enum": ["output", "cancel", "ps", "steer"]},
                "task_id": {"type": "string", "minLength": 1},
                "task_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 100, "uniqueItems": True},
                "block": {"type": "boolean"},
                "timeout": {"type": "integer", "minimum": 0},
                "full_session": {"type": "boolean"},
                "message_limit": {"type": "integer"},
            },
            "required": [],
            "anyOf": [
                {"required": ["prompt", "run_in_background", "load_skills", "subagent_type"]},
                {"required": ["operation"]},
            ],
            "examples": [
                {
                    "prompt": "Find where background task cancellation is implemented.",
                    "run_in_background": True,
                    "load_skills": [],
                    "subagent_type": "explore",
                },
                {
                    "prompt": "Review the architecture tradeoffs and summarize them.",
                    "run_in_background": False,
                    "load_skills": [],
                    "subagent_type": "advisor",
                },
            ],
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.SPAWN, ToolEffect.SESSION}),
        replay_policy="safe",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        context.require_session_id()
        command = context.task_runtime
        if command is None:
            raise RuntimeError("task requires a runtime-owned task command")
        return command(call, context=context)
