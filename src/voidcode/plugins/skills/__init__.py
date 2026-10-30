"""The ``skill://`` reader and ``skills`` command: workspace skill discovery and loading."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin, StateSchema
from voidcode.sdk.resources import ResourceReader

from .skills_plugin import (
    SkillReader,
    SkillsCommand,
    SkillsContextProvider,
    SkillShowCommand,
    parse_skills_config,
    prepare_skills_deactivation,
    skills_observer,
)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    skills_config = parse_skills_config(config)
    return Plugin(
        resource_readers=(ResourceReader("skill", SkillReader(skills_config).read),),
        commands=(SkillsCommand(skills_config), SkillShowCommand(skills_config)),
        context=SkillsContextProvider(skills_config),
        observer=skills_observer,
        observed_facts=("invocation_finished",),
        state=StateSchema(
            version=1,
            schema={
                "type": "object",
                "required": ["schema_version", "cursor_sequence", "touched_paths"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [1]},
                    "cursor_sequence": {"type": "integer", "minimum": 0},
                    "touched_paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        prepare_deactivate=prepare_skills_deactivation,
    )
