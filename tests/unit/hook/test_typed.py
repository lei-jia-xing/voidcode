from __future__ import annotations

from typing import cast

import pytest

from voidcode.hook.typed import (
    ToolInputDecision,
    ToolInputEvent,
    ToolInputHandlerBinding,
    ToolInputHandlerRegistry,
    builtin_tool_input_handler_registry,
    compose_tool_input_handler_registry,
    tool_input_rewrite_metadata,
    validate_tool_input_schema,
)
from voidcode.tools.contracts import ToolCall, ToolDefinition


def _event(arguments: dict[str, object]) -> ToolInputEvent:
    tool = ToolDefinition(
        name="read",
        description="read",
        input_schema={"path": {"type": "string"}, "required": ["path"]},
    )
    return ToolInputEvent(
        session_id="session-1",
        tool_call=ToolCall(tool_name="read", arguments=arguments),
        tool=tool,
        sequence=1,
        session_status="running",
        mode="normal",
        read_only=True,
    )


def test_tool_input_handlers_use_stable_priority_and_registration_order() -> None:
    calls: list[str] = []

    def handler(name: str, decision: ToolInputDecision):
        def run(event: ToolInputEvent) -> ToolInputDecision:
            calls.append(f"{name}:{event.tool_call.arguments['path']}")
            return decision

        return run

    registry = ToolInputHandlerRegistry(
        (
            ToolInputHandlerBinding("late", handler("late", ToolInputDecision(action="unchanged")), priority=10),
            ToolInputHandlerBinding("first", handler("first", ToolInputDecision(action="rewrite", arguments={"path": "./sample.txt"})), priority=0),
            ToolInputHandlerBinding("same", handler("same", ToolInputDecision(action="diagnostic", diagnostic="checked")), priority=10),
        )
    )
    outcome = registry.apply(event=_event({"path": "sample.txt"}))

    assert calls == ["first:sample.txt", "late:./sample.txt", "same:./sample.txt"]
    assert outcome.action == "rewrite"
    assert outcome.tool_call.arguments == {"path": "./sample.txt"}
    assert outcome.diagnostics == ("checked",)
    assert outcome.handler_names == ("first", "late", "same")


def test_tool_input_block_short_circuits_and_does_not_authorize() -> None:
    calls: list[str] = []

    def blocked(event: ToolInputEvent) -> ToolInputDecision:
        _ = event
        calls.append("blocked")
        return ToolInputDecision(action="block", reason="policy")

    def unreachable(event: ToolInputEvent) -> ToolInputDecision:
        _ = event
        calls.append("unreachable")
        return ToolInputDecision(action="unchanged")

    registry = ToolInputHandlerRegistry(
        (
            ToolInputHandlerBinding("blocked", blocked),
            ToolInputHandlerBinding("unreachable", unreachable),
        )
    )
    outcome = registry.apply(event=_event({"path": "sample.txt"}))

    assert outcome.action == "block"
    assert outcome.blocked_reason == "policy"
    assert calls == ["blocked"]


def test_tool_input_schema_gate_only_checks_published_shape() -> None:
    tool = ToolDefinition(
        name="read",
        description="read",
        input_schema={"path": {"type": "string"}, "required": ["path"]},
    )
    validate_tool_input_schema(tool, {"path": "sample.txt"})
    with pytest.raises(ValueError, match="input schema validation failed"):
        validate_tool_input_schema(tool, {"path": 123})


def test_handler_exception_fails_closed() -> None:
    def broken(event: ToolInputEvent) -> ToolInputDecision:
        _ = event
        raise RuntimeError("broken")

    outcome = ToolInputHandlerRegistry((ToolInputHandlerBinding("broken", broken),)).apply(event=_event({"path": "sample.txt"}))
    assert outcome.action == "block"
    assert outcome.blocked_reason is not None
    assert "broken" in outcome.blocked_reason


def test_rewrite_metadata_hashes_args_and_bounds_handler_outputs() -> None:
    def diagnostic(event: ToolInputEvent) -> ToolInputDecision:
        _ = event
        return ToolInputDecision(action="diagnostic", diagnostic="seen")

    registry = ToolInputHandlerRegistry(ToolInputHandlerBinding(f"handler-{index}", diagnostic) for index in range(64))
    outcome = registry.apply(event=_event({"path": "before.txt"}))
    metadata = tool_input_rewrite_metadata(original=_event({"path": "before.txt"}).tool_call, outcome=outcome)

    assert len(outcome.handler_names) <= 33
    assert len(cast(list[object], metadata["handler_names"])) <= 32
    assert len(cast(list[object], metadata["diagnostics"])) <= 32
    assert metadata["original_sha256"] == metadata["final_sha256"]


def test_builtin_registry_is_explicitly_empty_until_a_contract_is_proven() -> None:
    assert builtin_tool_input_handler_registry().bindings == ()


def test_composition_seam_keeps_builtin_before_configured_stable_order() -> None:
    def unchanged(event: ToolInputEvent) -> ToolInputDecision:
        _ = event
        return ToolInputDecision(action="unchanged")

    registry = compose_tool_input_handler_registry(
        (ToolInputHandlerBinding("builtin", unchanged, priority=0),),
        (ToolInputHandlerBinding("configured", unchanged, priority=10),),
    )
    assert tuple(binding.name for binding in registry.bindings) == ("builtin", "configured")
