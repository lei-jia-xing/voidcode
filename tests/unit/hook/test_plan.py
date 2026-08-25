from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.executor import LifecycleHookExecutionRequest, run_lifecycle_hooks
from voidcode.hook.plan import (
    HookPlanValidationError,
    ResolvedHookPlan,
    materialize_hook_plan,
)


def _command(marker: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-c",
        f"from pathlib import Path as P; P({str(marker)!r}).write_text('ran')",
    )


def test_materialized_plan_is_deterministic_and_preset_guidance_does_not_bind_commands(tmp_path: Path) -> None:
    command = _command(tmp_path / "marker")
    config = RuntimeHooksConfig(enabled=True, on_session_start=(command,))
    first = materialize_hook_plan(config, agent_hook_refs=("role_reminder",), agent_source="leader")
    second = materialize_hook_plan(config, agent_hook_refs=("role_reminder",), agent_source="leader")
    assert first.to_payload() == second.to_payload()
    assert first.plan_hash == second.plan_hash
    assert [binding.event for binding in first.bindings] == ["session_start"]
    assert first.bindings[0].command == command
    assert first.to_payload()["schema_version"] == 2
    assert all("handler_ref" not in binding and "priority" not in binding for binding in first.to_payload()["bindings"])
    assert first.metadata["preset_materialization"] == "guidance_only"
    assert first.metadata["agent_hook_refs"] == ["role_reminder"]


def test_materialized_plan_rejects_unknown_refs_duplicate_bindings_and_authority_scope() -> None:
    with pytest.raises(HookPlanValidationError, match="unknown hook preset"):
        materialize_hook_plan(RuntimeHooksConfig(), agent_hook_refs=("does_not_exist",))

    command = ("echo", "hook")
    with pytest.raises(HookPlanValidationError, match="duplicate hook binding"):
        materialize_hook_plan(RuntimeHooksConfig(on_session_start=(command, command)))

    with pytest.raises(HookPlanValidationError, match="scope"):
        materialize_hook_plan(RuntimeHooksConfig(), scope="agent")


def test_existing_executor_dispatches_commands_from_resolved_plan(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    plan = materialize_hook_plan(
        RuntimeHooksConfig(enabled=True, on_session_start=(_command(marker),)),
        agent_hook_refs=("delegation_guard",),
    )

    outcome = run_lifecycle_hooks(
        LifecycleHookExecutionRequest(
            hooks=None,
            plan=plan,
            workspace=tmp_path,
            session_id="session-1",
            surface="session_start",
            recursion_env_var="VOIDCODE_TEST_HOOK",
            environment={},
            sequence_start=0,
            payload={"safe": True},
        )
    )

    assert outcome.failed_error is None
    assert len(outcome.events) == 1
    assert marker.read_text() == "ran"


def test_persisted_plan_v2_roundtrip_hash_and_removed_fields_rejected(tmp_path: Path) -> None:
    plan = materialize_hook_plan(
        RuntimeHooksConfig(enabled=True, on_session_start=(_command(tmp_path / "marker"),)),
        agent_hook_refs=("role_reminder",),
    )
    payload = json.loads(json.dumps(plan.to_payload()))
    assert payload["schema_version"] == 2
    assert all("handler_ref" not in binding and "priority" not in binding for binding in payload["bindings"])
    restored = ResolvedHookPlan.from_payload(payload)
    assert restored.plan_hash == plan.plan_hash
    assert restored.metadata["agent_hook_refs"] == ["role_reminder"]

    payload["schema_version"] = 1
    with pytest.raises(HookPlanValidationError, match="schema_version"):
        ResolvedHookPlan.from_payload(payload)

    payload["schema_version"] = 2
    payload["bindings"][0]["priority"] = 0
    with pytest.raises(HookPlanValidationError, match="removed field"):
        ResolvedHookPlan.from_payload(payload)

    payload["bindings"][0].pop("priority")
    payload["bindings"][0]["handler_ref"] = "legacy"
    with pytest.raises(HookPlanValidationError, match="removed field"):
        ResolvedHookPlan.from_payload(payload)

    payload["bindings"][0].pop("handler_ref")
    payload["metadata"]["agent_hook_refs"] = ["catalog_removed_later"]
    with pytest.raises(HookPlanValidationError, match="hash"):
        ResolvedHookPlan.from_payload(payload)
