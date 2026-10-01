from __future__ import annotations

from ...core.tool_context import ToolContext
from ..contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult

MAX_BATCH_SIZE = 100


class TaskBatchTool:
    definition = ToolDefinition(
        name="task_batch",
        description=(
            "Dispatch a bounded batch of independent child sessions in the background under one "
            "runtime-owned parallel group. Each item must declare prompt, load_skills, and "
            "subagent_type. The runtime supplies parent ownership and group metadata; nested "
            "graphs, dependencies, retries, cancellation, and keep-alive are not supported."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_BATCH_SIZE,
                    "description": ("Independent child requests. All children run in the background and share one runtime-owned parallel group."),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "prompt": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Full delegated task prompt for this child session.",
                            },
                            "load_skills": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "description": "Skill names to force-load in this child; pass [] when none are needed.",
                            },
                            "subagent_type": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Explicit child preset: advisor, explore, researcher, worker, or product.",
                            },
                            "description": {"type": "string", "minLength": 1},
                            "command": {"type": "string", "minLength": 1},
                            "outputSchema": {
                                "type": "object",
                                "description": "Optional JSON Schema for this child's yield data.",
                            },
                            "schemaMode": {
                                "type": "string",
                                "enum": ["permissive", "strict"],
                                "description": "Validation mode for outputSchema; strict requires outputSchema.",
                            },
                        },
                        "required": ["prompt", "load_skills", "subagent_type"],
                    },
                }
            },
            "required": ["tasks"],
            "examples": [
                {
                    "tasks": [
                        {
                            "prompt": "Inspect the auth flow",
                            "load_skills": [],
                            "subagent_type": "explore",
                        },
                        {
                            "prompt": "Review the storage boundary",
                            "load_skills": [],
                            "subagent_type": "researcher",
                        },
                    ]
                }
            ],
        },
        effects=frozenset({ToolEffect.SPAWN, ToolEffect.SESSION}),
        replay_policy="safe",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        context.require_session_id()
        command = context.task_batch_runtime
        if command is None:
            raise RuntimeError("task_batch requires a runtime-owned task batch command")
        return command(call, context=context)
