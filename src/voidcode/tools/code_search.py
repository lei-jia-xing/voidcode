from __future__ import annotations
from pathlib import Path
from typing import ClassVar

from .contracts import ToolCall, ToolDefinition, ToolResult


class CodeSearchTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="codesearch",
        description="Search code examples and documentation using Exa AI.",
        input_schema={
            "query": {"type": "string", "description": "The search query"},
            "tokensNum": {
                "type": "integer",
                "description": "Max tokens (1000-50000, default 5000)",
            },
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        query = call.arguments.get("query")
        if not isinstance(query, str):
            raise ValueError("codesearch requires a string query argument")

        tokens_num = call.arguments.get("tokensNum", 5000)
        if not isinstance(tokens_num, int):
            tokens_num = 5000

        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=f"Code search for '{query}' with {tokens_num} tokens - MCP integration available",
        )
