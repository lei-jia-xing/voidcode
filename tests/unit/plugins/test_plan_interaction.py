"""Plan mode status offers an explicit next action without toggling on read."""

from pathlib import Path

from tests.fixtures.fake_services import FakeServices
from voidcode.plugins.plan.plan_plugin import PlanCommand, PlanOffCommand, PlanStatusCommand
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.views import FormViewData, validate_form_submission


def test_plan_status_exposes_only_the_explicit_next_mode_action(tmp_path: Path) -> None:
    services = FakeServices(owner="plan")
    context = ToolContext(tmp_path, "s", "r", "i", "v", "user")
    status = PlanStatusCommand().invoke(CommandInvocation(context, "plan.status"), services)
    form = FormViewData.from_payload(status.presentation.data)
    assert status.output == "Mode: default" and form.action == "plan"
    assert services.get_state()[0] is None
    entered = PlanCommand().invoke(CommandInvocation(context, form.action, validate_form_submission(form, {})), services)
    assert entered.data["active"] is True
    leave = FormViewData.from_payload(entered.presentation.data)
    assert leave.action == "plan.off"
    status = PlanStatusCommand().invoke(CommandInvocation(context, "plan.status"), services)
    assert status.output == "Mode: plan"
    off = PlanOffCommand().invoke(CommandInvocation(context, leave.action, validate_form_submission(leave, {})), services)
    assert off.data["active"] is False
    assert off.presentation.data["action"] == "plan"


def test_plan_factory_only_accepts_injected_provider_config() -> None:
    import pytest

    from voidcode.plugins.plan import create_plugin

    assert len(create_plugin({"providers": {}}).commands) == 3
    with pytest.raises(ValueError, match="plan config does not support field: enabled"):
        create_plugin({"enabled": True})


def test_plan_state_ingress_rejects_coercion_and_missing_fields() -> None:
    import pytest

    from voidcode.plugins.plan.plan_plugin import PlanState

    for version in (True, 1.0, "1", 0, 2):
        with pytest.raises(ValueError, match="schema_version"):
            PlanState.from_payload({"schema_version": version, "active": False})
    for active in (0, 1, "false", None):
        with pytest.raises(ValueError, match="boolean"):
            PlanState.from_payload({"schema_version": 1, "active": active})
    for payload in ({}, {"active": False}, {"schema_version": 1}):
        with pytest.raises(ValueError, match="missing"):
            PlanState.from_payload(payload)
    with pytest.raises(ValueError, match="not supported"):
        PlanState.from_payload({"schema_version": 1, "active": False, "other": 1})
    assert PlanState.from_payload(None).active is False
    assert PlanState.from_payload({"schema_version": 1, "active": True}).active is True


def test_plan_deactivation_validates_and_parks_off_without_changing_input() -> None:
    import pytest

    from voidcode.plugins.plan.plan_plugin import prepare_plan_deactivation

    state = {"schema_version": 1, "active": True}
    parked = prepare_plan_deactivation(state, "session", "s", 12)
    assert parked == {"schema_version": 1, "active": False}
    assert state == {"schema_version": 1, "active": True}
    with pytest.raises(ValueError, match="boolean"):
        prepare_plan_deactivation({"schema_version": 1, "active": "yes"}, "session", "s", 12)
