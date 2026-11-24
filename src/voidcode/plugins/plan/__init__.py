"""Plan mode: an owner-scoped restrictive overlay plus ``plan`` commands."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .plan_plugin import PlanCommand, PlanOffCommand, PlanStatusCommand, plan_context


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    unknown = sorted(set(config or {}) - {"providers"})
    if unknown:
        raise ValueError(f"plan config does not support field: {unknown[0]}")
    return Plugin(
        commands=(PlanCommand(), PlanOffCommand(), PlanStatusCommand()),
        context=plan_context,
    )
