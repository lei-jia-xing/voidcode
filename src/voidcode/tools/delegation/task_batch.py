from __future__ import annotations

from ...core.tool_context import ToolContext
from ..contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult

MAX_BATCH_SIZE = 100


class TaskBatchTool:
    definition = ToolDefinition(
        name="task_batch",
        description="Start independent child sessions together as a bounded runtime-owned background group.",
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
                                "description": (
                                    "Explicit child preset ID: review, explore, research, execute, or plan. "
                                    "Use review for evidence review, explore for workspace discovery, "
                                    "research for external research, execute for delegated execution, and plan for read-only planning."
                                ),
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
                            "prompt": "Find the visitor center's opening hours.",
                            "load_skills": [],
                            "subagent_type": "explore",
                        },
                        {
                            "prompt": "Summarize transport options from the visitor guide.",
                            "load_skills": [],
                            "subagent_type": "research",
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
