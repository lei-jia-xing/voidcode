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
        # Static description: the skill catalog (name + description per skill)
        # lives in system-prompt metadata (see catalog_skill_context in the
        # runtime), not in this tool's description. The SKILL.md body is served
        # on demand by invoke(). Keeping the description static avoids shipping
        # the catalog on every provider request.
        _ = self._list_skills
        return ToolDefinition(
            name="skill",
            description=(
                "Load a runtime-discovered skill into the current conversation context.\n"
                "\n"
                "Usage:\n"
                "- Use this tool when the task matches a skill in the runtime skills catalog "
                "listed in the system prompt metadata.\n"
                "- The name argument is required and must match a catalog skill name.\n"
                "- The tool returns the resolved SKILL.md body and metadata so the agent can "
                "follow it in the current turn.\n"
                "- This tool does not create or edit skills; it only loads already-discovered "
                "local skills.\n"
                "- If the skill is unknown, the tool fails instead of guessing."
            ),
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
