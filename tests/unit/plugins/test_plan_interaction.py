"""Plan mode is its overlay: status and transitions never read a second state truth."""

from pathlib import Path

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.plan.plan_plugin import (
    DISABLED_OUTPUT,
    READ_ONLY_AFTER_PLAN,
    PlanCommand,
    PlanOffCommand,
    PlanStatusCommand,
    plan_context,
)
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.views import FormViewData, validate_form_submission


def _context(tmp_path: Path) -> ToolContext:
    return ToolContext(tmp_path, "s", "r", "i", "v", "user")


def test_plan_status_exposes_only_the_explicit_next_mode_action(tmp_path: Path) -> None:
    services = FakeServices(owner="plan")
    context = _context(tmp_path)
    status = PlanStatusCommand().invoke(CommandInvocation(context, "plan.status"), services)
    form = FormViewData.from_payload(status.presentation.data)
    assert status.output == "Plan mode: inactive"
    assert status.data == {"active": False, "read_only": False}
    assert form.action == "plan"
    assert services.overlay is None

    entered = PlanCommand().invoke(CommandInvocation(context, form.action, validate_form_submission(form, {})), services)
    assert entered.data == {"active": True, "changed": True}
    assert services.overlay == "read-only"
    assert services.staged_events == [("plan_mode_enabled", {})]
    leave = FormViewData.from_payload(entered.presentation.data)
    assert leave.action == "plan.off"

    status = PlanStatusCommand().invoke(CommandInvocation(context, "plan.status"), services)
    assert status.output == "Plan mode: active"
    assert status.data == {"active": True, "read_only": True}
    off = PlanOffCommand().invoke(CommandInvocation(context, leave.action, validate_form_submission(leave, {})), services)
    assert off.data == {"active": False, "changed": True}
    assert services.overlay is None
    assert services.staged_events[-1] == ("plan_mode_disabled", {})
    assert off.presentation.data["action"] == "plan"

    # Repeating either transition is idempotent and stages no duplicate event.
    events = list(services.staged_events)
    again = PlanOffCommand().invoke(CommandInvocation(context, "plan.off"), services)
    assert again.data == {"active": False, "changed": False}
    assert services.staged_events == events


def test_plan_status_notes_another_read_only_reason_without_claiming_plan_mode(tmp_path: Path) -> None:
    services = FakeServices(owner="plan", read_only=True)
    status = PlanStatusCommand().invoke(CommandInvocation(_context(tmp_path), "plan.status"), services)
    assert status.output == f"Plan mode: inactive\n{READ_ONLY_AFTER_PLAN}"
    assert status.data == {"active": False, "read_only": True}

    off = PlanOffCommand().invoke(CommandInvocation(_context(tmp_path), "plan.off"), services)
    assert off.output == f"{DISABLED_OUTPUT}\n{READ_ONLY_AFTER_PLAN}"


def test_plan_ignores_legacy_plugin_state(tmp_path: Path) -> None:
    services = FakeServices(owner="plan")
    services.seed_state({"schema_version": 99, "active": "not-a-boolean"})
    assert PlanStatusCommand().invoke(CommandInvocation(_context(tmp_path), "plan.status"), services).output == "Plan mode: inactive"
    entered = PlanCommand().invoke(CommandInvocation(_context(tmp_path), "plan"), services)
    assert entered.data["changed"] is True and services.overlay == "read-only"
    assert plan_context(_context(tmp_path), services) is not None
    assert PlanOffCommand().invoke(CommandInvocation(_context(tmp_path), "plan.off"), services).data["changed"] is True


def test_plan_factory_only_accepts_injected_provider_config_and_owns_no_state() -> None:
    import pytest

    from voidcode.plugins.plan import create_plugin

    plugin = create_plugin({"providers": {}})
    assert len(plugin.commands) == 3
    assert plugin.prepare_deactivate is None
    with pytest.raises(ValueError, match="plan config does not support field: enabled"):
        create_plugin({"enabled": True})
