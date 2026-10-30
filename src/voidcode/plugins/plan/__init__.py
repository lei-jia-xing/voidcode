"""Plan mode: a plugin-owned restrictive overlay plus ``plan`` commands."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin, StateSchema

from .plan_plugin import SCHEMA_VERSION, PlanCommand, PlanOffCommand, PlanStatusCommand, plan_context, prepare_plan_deactivation


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    unknown = sorted(set(config or {}) - {"providers"})
    if unknown:
        raise ValueError(f"plan config does not support field: {unknown[0]}")
    return Plugin(
        commands=(PlanCommand(), PlanOffCommand(), PlanStatusCommand()),
        context=plan_context,
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "active"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "active": {"type": "boolean"},
                },
            },
        ),
        prepare_deactivate=prepare_plan_deactivation,
    )
