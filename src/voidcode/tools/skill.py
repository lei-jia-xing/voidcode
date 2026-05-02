from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator

from ..skills.models import SkillMetadata
from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult


class _SkillArgs(BaseModel):
    name: str
    user_message: str | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must be a non-empty string")
        return stripped


class SkillTool:
    def __init__(
        self,
        *,
        list_skills: Callable[[], tuple[SkillMetadata, ...]],
        resolve_skill: Callable[[str], SkillMetadata],
    ) -> None:
        self._list_skills = list_skills
        self._resolve_skill = resolve_skill

    @property
    def definition(self) -> ToolDefinition:
        available = self._list_skills()
        description_lines = [
            "Load a runtime-discovered skill into the current conversation context.",
            "",
            "Usage:",
            "- Use this tool when the task matches a discovered SKILL.md workflow.",
            "- The name argument is required and must match one of the available skills below.",
            "- The tool returns the resolved skill body and metadata so the agent can follow it in the current turn.",  # noqa: E501
            "- This tool does not create or edit skills; it only loads already-discovered local skills.",  # noqa: E501
            "- If the skill is unknown, the tool fails instead of guessing.",
            "",
            "<available_skills>",
        ]
        if available:
            for skill in available:
                description_lines.extend(
                    (
                        "  <skill>",
                        f"    <name>{skill.name}</name>",
                        f"    <description>{skill.description}</description>",
                        f"    <location>{skill.entry_path.as_uri()}</location>",
                        "  </skill>",
                    )
                )
        else:
            description_lines.append("  <skill><name>(none discovered)</name></skill>")
        description_lines.append("</available_skills>")
        return ToolDefinition(
            name="skill",
            description="\n".join(description_lines),
            input_schema={
                "name": {"type": "string", "description": "Skill name to load."},
                "user_message": {
                    "type": "string",
                    "description": "Optional command arguments or extra context for the skill.",
                },
            },
            read_only=True,
        )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            args = _SkillArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error("skill", exc)) from exc

        skill = self._resolve_skill(args.name)
        content_lines = [
            f"## Skill: {skill.name}",
            f"**Description**: {skill.description}",
            f"**Base directory**: {skill.directory}",
            f"**Entry path**: {skill.entry_path}",
            "",
            skill.content.strip(),
        ]
        if args.user_message:
            content_lines.extend(("", f"User message: {args.user_message}"))
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content="\n".join(content_lines).strip(),
            data={
                "skill": {
                    "name": skill.name,
                    "description": skill.description,
                    "source_path": str(skill.entry_path),
                    "directory": str(skill.directory),
                    "content": skill.content,
                },
                **({"user_message": args.user_message} if args.user_message is not None else {}),
            },
        )
