from __future__ import annotations

from typing import cast

import pytest

from voidcode.hook import (
    RewriteDecision,
    ToolInputEvent,
    ToolInputHandlerBinding,
    ToolInputHandlerDeclaration,
    ToolInputHandlerRegistry,
    tool_input_rewrite_metadata,
)
from voidcode.tools.contracts import ToolCall
from voidcode.tools.shell_exec import ShellExecTool


class _Append:
    version = "2"

    def __init__(self, step: int) -> None:
        self.step = step
        self.declaration = self.declare(step)

    @classmethod
    def declare(cls, step: int) -> ToolInputHandlerDeclaration:
        return ToolInputHandlerDeclaration(f"append-{step}", cls.version, 0)

    def __call__(self, event: ToolInputEvent) -> RewriteDecision:
        return RewriteDecision(arguments={"command": f"{event.tool_call.arguments['command']}|{self.step}"})


class _Double:
    declaration = ToolInputHandlerDeclaration("double", "3", 0)

    def __call__(self, event: ToolInputEvent) -> RewriteDecision:
        return RewriteDecision(arguments={"command": str(event.tool_call.arguments["command"]) * 2})


def _event() -> ToolInputEvent:
    return ToolInputEvent(
        session_id="consumer",
        tool_call=ToolCall("shell_exec", {"command": "seed"}),
        tool=ShellExecTool.definition,
        sequence=1,
        session_status="active",
        mode="normal",
        read_only=False,
    )


def test_equal_priority_registration_order_changes_final_arguments() -> None:
    append = _Append(0)
    double = _Double()
    bindings = (ToolInputHandlerBinding(append.declaration, append), ToolInputHandlerBinding(double.declaration, double))
    forward = ToolInputHandlerRegistry(bindings).apply(event=_event())
    reverse = ToolInputHandlerRegistry(reversed(bindings)).apply(event=_event())
    assert forward.tool_call.arguments["command"] == "seed|0seed|0"
    assert reverse.tool_call.arguments["command"] == "seedseed|0"
    metadata = tool_input_rewrite_metadata(original=_event().tool_call, outcome=forward)
    assert metadata["version"] == 2
    assert metadata["handlers"] == [
        {"name": "append-0", "version": "2", "priority": 0},
        {"name": "double", "version": "3", "priority": 0},
    ]
    assert metadata["original_sha256"] != metadata["final_sha256"]


def test_bounded_records_do_not_truncate_execution_or_forge_omitted_handler() -> None:
    declarations = tuple(_Append.declare(step) for step in range(35))
    factories = {declaration.name: step for step, declaration in enumerate(declarations)}

    def materialize(name: str) -> ToolInputHandlerBinding:
        handler = _Append(factories[name])
        return ToolInputHandlerBinding(handler.declaration, handler)

    outcome = ToolInputHandlerRegistry.from_declarations(declarations).bind(materialize).apply(event=_event())
    assert outcome.tool_call.arguments["command"] == "seed" + "".join(f"|{step}" for step in range(35))
    assert outcome.handlers == declarations[:32]
    assert outcome.omitted_handler_count == 3
    assert outcome.metadata_payload()["omitted_handler_count"] == 3


def test_unbound_and_wrong_actual_binding_are_refused() -> None:
    registry = ToolInputHandlerRegistry.from_declarations((_Double.declaration,))
    with pytest.raises(RuntimeError):
        registry.apply(event=_event())
    append = _Append(0)
    with pytest.raises(ValueError):
        registry.bind(lambda _name: ToolInputHandlerBinding(append.declaration, append))


@pytest.mark.parametrize("version", [True, 1, "", None])
def test_non_string_input_implementation_version_is_refused(version: object) -> None:
    with pytest.raises(ValueError):
        ToolInputHandlerDeclaration("invalid-version", cast(str, version))
