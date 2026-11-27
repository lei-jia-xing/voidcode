"""The ``/skills`` browser and paged ``/skills.show`` detail command."""

from __future__ import annotations

import json
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

from .config import SkillsConfig
from .discovery import SkillLoadFailure
from .limits import bounded_message
from .registry import build_registry


class SkillsCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="skills",
        description="List discovered workspace skills and whether each is enabled.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        config = self._config
        if not config.enabled:
            return CommandResult(
                status="ok",
                output="Skills are disabled.",
                data={"enabled": False, "skills": []},
                presentation=_skills_notice("Skills are disabled. Set plugins.skills.config.enabled to true in voidcode.toml and run /core reload."),
            )
        workspace = invocation.context.require_workspace()
        try:
            registry = build_registry(workspace=workspace, config=config)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))

        rows: list[dict[str, object]] = [
            {
                "name": skill.name,
                "description": skill.description,
                "enabled": skill.enabled,
                "status": "enabled" if skill.enabled else "disabled",
                "entry_path": str(skill.entry_path),
                "directory": str(skill.directory),
                "error": None,
                "recovery": None,
            }
            for skill in sorted(registry.all(), key=lambda item: item.name)
        ]
        rows.extend(
            {
                "name": failure.display_name,
                "description": "",
                "enabled": False,
                "status": "broken",
                "entry_path": str(failure.path),
                "directory": str(failure.path.parent),
                "error": failure.message,
                "recovery": _broken_skill_recovery(failure),
            }
            for failure in registry.failures
        )
        rows.sort(key=lambda row: str(row["name"]))
        if not rows:
            output = "No skills discovered."
        else:
            lines = []
            for row in rows:
                if row["status"] == "broken":
                    lines.append(f"- [broken] {row['name']} — {row['error']} ({row['entry_path']}); {row['recovery']}")
                else:
                    lines.append(f"- [{row['status']}] {row['name']} — {row['description']} ({row['entry_path']})")
            if registry.config_errors:
                lines.extend(("", "Configuration diagnostics:"))
                lines.extend(f"- {error}" for error in registry.config_errors)
            output = "\n".join(lines)
        options = [
            {
                "id": str(row["name"]),
                "label": (
                    f"{row['name']} — broken · {str(row['error'])[:160]}"
                    if row["status"] == "broken"
                    else f"{row['name']} — {row['status']} · {str(row['description'])[:200]}"
                ),
            }
            for row in rows[:100]
        ]
        presentation = (
            CommandPresentation(
                title=f"Skills ({min(len(rows), 100)} of {len(rows)})",
                view_type="choices",
                data={"options": options, "action": "skills.show", "argument_name": "name"},
            )
            if options
            else _skills_notice("No skills discovered. Add a SKILL.md under .voidcode/skills/<name>/, then run /skills again.")
        )
        return CommandResult(
            status="ok",
            output=output,
            data={"enabled": True, "skills": rows, "diagnostics": list(registry.config_errors)},
            presentation=presentation,
        )


def _broken_skill_recovery(failure: SkillLoadFailure) -> str:
    return f'fix {failure.path} or disable it in plugins.skills.config with {{"path": "<that path>", "enabled": false}}, then run /core reload'


def _skills_notice(message: str) -> CommandPresentation:
    return CommandPresentation(title="Skills", view_type="notice", data={"message": message})


class SkillShowCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="skills.show",
        description="Inspect a skill's source, enabled state and paged instructions.",
        text_argument="name",
        effects=frozenset({ToolEffect.READ}),
        argument_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "offset": {"type": "integer", "minimum": 0, "description": "Character offset from a previous page."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 6000},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )

    def __init__(self, config: SkillsConfig) -> None:
        self._config = config

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = services
        if not self._config.enabled:
            return _skills_notice("Skills are disabled. Enable plugins.skills.config.enabled in voidcode.toml and run /core reload.")
        try:
            registry = build_registry(workspace=invocation.context.require_workspace(), config=self._config)
        except (ValueError, OSError) as exc:
            return _skills_notice(f"Skills could not be discovered: {bounded_message(exc)}")
        skills = sorted(registry.all(), key=lambda skill: skill.name)
        options = [{"id": skill.name, "label": f"{skill.name} — {skill.description[:200]}"} for skill in skills]
        options.extend(
            {"id": failure.display_name, "label": f"{failure.display_name} — broken · {failure.message[:200]}"} for failure in registry.failures
        )
        if not options:
            if registry.config_errors:
                return _skills_notice("Skills could not be discovered: " + "; ".join(registry.config_errors))
            return _skills_notice("No skills discovered. Add a SKILL.md under .voidcode/skills/<name>/ and retry.")
        return CommandPresentation(
            title=f"Inspect skill ({min(len(options), 100)} of {len(options)})",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "name",
                        "label": "Skill",
                        "options": options[:100],
                    }
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = services
        if not self._config.enabled:
            return CommandResult(status="error", error="Skills are disabled. Enable plugins.skills.config.enabled in voidcode.toml and reload.")
        name = invocation.arguments.get("name")
        offset, limit = invocation.arguments.get("offset", 0), invocation.arguments.get("limit", 4000)
        if not isinstance(name, str) or not name.strip():
            return CommandResult(status="error", error="skills.show requires a skill name")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000:
            return CommandResult(status="error", error="offset must be non-negative and limit must be between 1 and 6000")
        try:
            registry = build_registry(workspace=invocation.context.require_workspace(), config=self._config)
        except (ValueError, OSError) as exc:
            return CommandResult(status="error", error=str(exc))
        skill = registry.skills.get(name.strip())
        if skill is None:
            failure = registry.failure(name.strip())
            if failure is not None:
                return CommandResult(
                    status="error",
                    error=f"Skill '{failure.display_name}' is broken: {failure.message}. {_broken_skill_recovery(failure)}.",
                )
            return CommandResult(status="error", error=f"Unknown skill: {name}")
        if offset > len(skill.content):
            return CommandResult(status="error", error="Skill offset is beyond the end of its instructions")
        end = min(offset + limit, len(skill.content))
        while True:
            content = (
                f"# {skill.name[:256]}\n\n{skill.description[:800]}\n\n"
                f"Status: {'enabled' if skill.enabled else 'disabled'}\n\nSource: `{str(skill.entry_path)[:1500]}`\n\n"
                f"Instructions (characters {offset}–{end} of {len(skill.content)}):\n\n{skill.content[offset:end]}"
            )
            actions: list[dict[str, object]] = []
            if offset:
                actions.append(
                    {
                        "label": "Previous page",
                        "action": "skills.show",
                        "arguments": {"name": skill.name, "offset": max(0, offset - limit), "limit": limit},
                    }
                )
            if end < len(skill.content):
                actions.append({"label": "Next page", "action": "skills.show", "arguments": {"name": skill.name, "offset": end, "limit": limit}})
            actions.append({"label": "Back to skills", "action": "skills"})
            # Exact SDK acceptance: validate_view_data measures this same data mapping
            # with json.dumps(json_wire_object(data), allow_nan=False); no headroom fudge.
            if len(json.dumps(json_wire_object({"content": content, "actions": actions}), allow_nan=False).encode("utf-8")) <= MAX_VIEW_DATA_BYTES:
                break
            if end <= offset + 1:
                return CommandResult(status="error", error="Skill metadata exceeds the detail view limit; shorten its name or description")
            end = offset + max(1, (end - offset) // 2)
        return CommandResult(
            status="ok",
            output=content,
            data={
                "name": skill.name,
                "enabled": skill.enabled,
                "offset": offset,
                "offset_unit": "characters",
                "next_offset": end if end < len(skill.content) else None,
                "total_characters": len(skill.content),
            },
            presentation=CommandPresentation(title=f"Skill: {skill.name[:256]}", view_type="markdown", data={"content": content, "actions": actions}),
        )
