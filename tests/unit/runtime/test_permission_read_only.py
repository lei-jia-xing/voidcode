from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

from tests.turn_producers import model_turn_fact
from voidcode.core.turns import FinalTurn, ToolTurn, TurnRequest
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.hook.typed import ToolInputHandlerRegistry
from voidcode.runtime.composition import CompositionRef, TaskCompositionOwner
from voidcode.runtime.config.loading import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import (
    RuntimeRequest,
    RuntimeRequestError,
    validate_runtime_request_metadata,
)
from voidcode.runtime.permissions.context import RuntimePermissionContextResolver, operation_class_for_tool
from voidcode.runtime.permissions.engine import PermissionEngine
from voidcode.runtime.permissions.policy import (
    DEFAULT_APPROVAL_MODE,
    READ_ONLY_DENIAL_REASON,
    ExternalDirectoryPermissionConfig,
    ExternalDirectoryPolicy,
    OperationClass,
    PathScope,
    PermissionPolicy,
    approval_decision,
    is_read_only_blocked,
    resolve_permission,
    runtime_read_only_from_metadata,
)
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.tools.registry import ToolRegistry
from voidcode.tools.apply_workspace_edit import ApplyWorkspaceEditTool
from voidcode.tools.ast_grep import AstGrepTool
from voidcode.tools.contracts import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolResult, ToolSuccess
from voidcode.tools.shell_exec import ShellExecTool


def _read_only_tool() -> ToolDefinition:
    return ToolDefinition(
        name="grep",
        description="search read-only tool",
        input_schema={},
        effects=frozenset({ToolEffect.READ}),
    )


def _write_tool() -> ToolDefinition:
    return ToolDefinition(
        name="write",
        description="mutating tool",
        input_schema={},
        effects=frozenset({ToolEffect.WRITE}),
    )


def _call(name: str = "write") -> ToolCall:
    return ToolCall(tool_name=name, arguments={"path": "foo.txt"})


@pytest.mark.parametrize(
    ("read_only", "tool", "operation_class", "expected"),
    [
        (False, _write_tool(), "write", False),
        (True, _read_only_tool(), "read", False),
        (True, _read_only_tool(), None, False),
        (True, _write_tool(), "write", True),
        (True, _write_tool(), None, True),
        # Defense in depth: a read_only tool asked to perform a write/execute
        # operation is still denied while the read-only stance is active.
        (True, _read_only_tool(), "write", True),
        (True, _read_only_tool(), "execute", True),
    ],
)
def test_is_read_only_blocked_matrix(
    read_only: bool,
    tool: ToolDefinition,
    operation_class: str | None,
    expected: bool,
) -> None:
    assert (
        is_read_only_blocked(
            read_only=read_only,
            tool=tool,
            operation_class=cast(OperationClass | None, operation_class),
        )
        is expected
    )


def test_resolve_permission_read_only_denies_write_tool() -> None:
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        read_only=True,
    )

    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_mode == "deny"
    assert outcome.pending_approval.policy_surface == "read_only"
    assert outcome.pending_approval.reason == READ_ONLY_DENIAL_REASON


def test_resolve_permission_read_only_denies_shell_execute_operation() -> None:
    shell_tool = ShellExecTool()
    shell = shell_tool.definition
    call = ToolCall(tool_name="shell_exec", arguments={"command": "pwd"})
    operation = operation_class_for_tool(
        call.tool_name,
        shell.effects,
        tool_instance=shell_tool,
        arguments=call.arguments,
    )
    outcome = resolve_permission(
        shell,
        call,
        policy=PermissionPolicy(mode="yolo"),
        operation_class=operation,
        read_only=True,
    )

    assert operation == "execute"
    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "read_only"
    assert outcome.pending_approval.operation_class == "execute"


def test_resolve_permission_read_only_allows_read_only_tool() -> None:
    outcome = resolve_permission(
        _read_only_tool(),
        _call("grep"),
        policy=PermissionPolicy(mode="ask"),
        read_only=True,
    )

    assert outcome.decision == "allow"
    assert outcome.pending_approval is None


def test_resolve_permission_without_read_only_does_not_short_circuit() -> None:
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        read_only=False,
    )

    assert outcome.decision == "ask"
    assert outcome.pending_approval is not None
    # Without read-only policy the default approval reason remains in force.
    assert outcome.pending_approval.reason != READ_ONLY_DENIAL_REASON


def test_resolve_permission_read_only_overrides_explicit_allow_rule() -> None:
    """The read-only stance is a hard ceiling; even an `allow` rule cannot override it."""
    outcome = resolve_permission(
        _write_tool(),
        _call(),
        policy=PermissionPolicy(mode="ask"),
        rule_decision="allow",
        read_only=True,
    )

    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "read_only"


def test_request_metadata_rejects_invalid_read_only_flag() -> None:
    with pytest.raises(RuntimeRequestError, match="read_only"):
        validate_runtime_request_metadata({"read_only": "true"})


def test_internal_request_metadata_preserves_discriminated_composition_owner(tmp_path: Path) -> None:
    ref = CompositionRef(
        workspace=str(tmp_path),
        owner=TaskCompositionOwner(kind="task", task_id="task-1"),
        binding_id="binding-1",
        plan_id="plan-1",
    )
    payload = ref.model_dump(mode="json")

    with pytest.raises(RuntimeRequestError, match="unsupported request metadata field.*composition_ref"):
        validate_runtime_request_metadata({"composition_ref": payload})

    normalized = validate_runtime_request_metadata({"composition_ref": payload}, allow_internal_fields=True)

    assert normalized["composition_ref"] == payload
    assert CompositionRef.model_validate(normalized["composition_ref"]) == ref


@pytest.mark.parametrize("read_only", [True, False])
def test_request_metadata_accepts_explicit_read_only_flag(read_only: bool) -> None:
    normalized = validate_runtime_request_metadata({"read_only": read_only})

    assert normalized.get("read_only") is read_only
    assert runtime_read_only_from_metadata(normalized) is read_only


def test_runtime_read_only_from_metadata_snapshot_authority() -> None:
    # Snapshot True is authoritative; explicit false cannot relax it
    assert runtime_read_only_from_metadata({"runtime_policy": {"read_only": True}}) is True
    assert runtime_read_only_from_metadata({"read_only": False, "runtime_policy": {"read_only": True}}) is True

    # Request True can narrow a writable policy snapshot
    assert runtime_read_only_from_metadata({"read_only": True, "runtime_policy": {"read_only": False}}) is True

    # Writable stays writable
    assert runtime_read_only_from_metadata({"read_only": False, "runtime_policy": {"read_only": False}}) is False
    assert runtime_read_only_from_metadata({"runtime_policy": {"read_only": False}}) is False


class _DynamicWriteProbe:
    definition = ToolDefinition(
        name="write_probe",
        description="write a test file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        effects=frozenset({ToolEffect.WRITE}),
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls: list[ToolCall] = []

    def invoke(self, call: ToolCall, *, context: object) -> ToolResult:
        _ = context
        self.calls.append(call)
        out = self.workspace / str(call.arguments["path"])
        out.write_text("written", encoding="utf-8")
        return ToolSuccess(tool_name=call.tool_name, output=TextOutput("written"))


class _PromptScriptedProducer:
    def __init__(self, *, write_on_prompts: tuple[str, ...] | None = None) -> None:
        self.executed_prompts: set[str] = set()
        self.write_on_prompts = write_on_prompts

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: object,
    ) -> ToolTurn | FinalTurn:
        _ = session, tool_results
        prompt = request.prompt
        fact = model_turn_fact(request)
        if self.write_on_prompts is not None:
            should_write = prompt in self.write_on_prompts
        else:
            should_write = not prompt.startswith("safe")
        if should_write and prompt not in self.executed_prompts:
            self.executed_prompts.add(prompt)
            path = f"{prompt.replace(' ', '_')}.txt"
            return ToolTurn(calls=(ToolCall(tool_name="write_probe", arguments={"path": path}),), facts=(fact,))
        return FinalTurn(output=f"{prompt} done", facts=(fact,))


@pytest.mark.parametrize("turn_2_metadata", [{}, {"read_only": False}])
def test_read_only_session_persists_across_turns_and_cannot_be_relaxed(tmp_path: Path, turn_2_metadata: dict[str, object]) -> None:
    probe = _DynamicWriteProbe(tmp_path)
    producer = _PromptScriptedProducer(write_on_prompts=("turn 2",))
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([probe]),
        turn_producer=producer,
        config=RuntimeConfig(approval_mode="yolo", mcp=RuntimeMcpConfig(enabled=False)),
        permission_policy=PermissionPolicy(mode="yolo"),
        tool_input_handler_registry=ToolInputHandlerRegistry(()),
    )
    # Turn 1: session initialized with read_only=True
    res1 = runtime.run(RuntimeRequest(prompt="turn 1", metadata={"read_only": True}))
    assert res1.session.metadata.get("read_only") is True
    assert (tmp_path / "turn_1.txt").exists() is False
    assert len(probe.calls) == 0

    session_id = res1.session.session.id

    # Turn 2: follow-up request on same session without read_only or with read_only: False
    with pytest.raises(ValueError, match="read-only access is active"):
        runtime.run(RuntimeRequest(prompt="turn 2", session_id=session_id, metadata=turn_2_metadata))

    assert (tmp_path / "turn_2.txt").exists() is False
    assert len(probe.calls) == 0


def test_child_session_inherits_read_only_and_cannot_be_relaxed(tmp_path: Path) -> None:
    probe = _DynamicWriteProbe(tmp_path)
    producer = _PromptScriptedProducer(write_on_prompts=("child turn",))
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([probe]),
        turn_producer=producer,
        config=RuntimeConfig(approval_mode="yolo", mcp=RuntimeMcpConfig(enabled=False)),
        permission_policy=PermissionPolicy(mode="yolo"),
        tool_input_handler_registry=ToolInputHandlerRegistry(()),
    )
    res_parent = runtime.run(RuntimeRequest(prompt="parent turn", metadata={"read_only": True}))
    parent_id = res_parent.session.session.id

    with pytest.raises(ValueError, match="read-only access is active"):
        runtime.run(RuntimeRequest(prompt="child turn", parent_session_id=parent_id, metadata={"read_only": False}))

    assert (tmp_path / "child_turn.txt").exists() is False
    assert len(probe.calls) == 0


def test_writable_session_allows_mutations_and_permits_turn_narrowing(tmp_path: Path) -> None:
    probe = _DynamicWriteProbe(tmp_path)
    producer = _PromptScriptedProducer()
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([probe]),
        turn_producer=producer,
        config=RuntimeConfig(approval_mode="yolo", mcp=RuntimeMcpConfig(enabled=False)),
        permission_policy=PermissionPolicy(mode="yolo"),
        tool_input_handler_registry=ToolInputHandlerRegistry(()),
    )
    # Turn 1: Writable session
    res1 = runtime.run(RuntimeRequest(prompt="turn 1"))
    session_id = res1.session.session.id
    assert (tmp_path / "turn_1.txt").exists() is True

    # Turn 2: Narrowed to read_only: True for this turn
    with pytest.raises(ValueError, match="read-only access is active"):
        runtime.run(RuntimeRequest(prompt="turn 2", session_id=session_id, metadata={"read_only": True}))
    assert (tmp_path / "turn_2.txt").exists() is False

    # Turn 3: Writable again
    res3 = runtime.run(RuntimeRequest(prompt="turn 3", session_id=session_id))
    assert res3.session.status == "completed"
    assert (tmp_path / "turn_3.txt").exists() is True
    assert len(probe.calls) == 2


def test_read_only_session_denies_lifecycle_hook_mutations(tmp_path: Path) -> None:
    probe = _DynamicWriteProbe(tmp_path)
    producer = _PromptScriptedProducer(write_on_prompts=())
    hook_marker = tmp_path / "hook_mutated.txt"
    hook_script = f"import pathlib; pathlib.Path({str(hook_marker)!r}).write_text('mutated')"

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        tool_registry=ToolRegistry.from_tools([probe]),
        turn_producer=producer,
        config=RuntimeConfig(
            approval_mode="yolo",
            mcp=RuntimeMcpConfig(enabled=False),
            hooks=RuntimeHooksConfig(
                enabled=True,
                on_session_end=((sys.executable, "-c", hook_script),),
            ),
        ),
        permission_policy=PermissionPolicy(mode="yolo"),
        tool_input_handler_registry=ToolInputHandlerRegistry(()),
    )
    # Turn 1: safe completed request under read_only: True
    res1 = runtime.run(RuntimeRequest(prompt="safe turn 1", session_id="ro-hook-session", metadata={"read_only": True}))
    assert res1.session.status == "completed"
    assert hook_marker.exists() is False
    skip1 = next(e for e in res1.events if e.event_type == "runtime.lifecycle_hook")
    assert skip1.payload["hook_status"] == "skipped"
    assert skip1.payload["hook_policy"]["read_only"] is True
    assert skip1.payload["hook_policy"]["outcome"] == "skipped"

    # Turn 2: safe completed request on same session with explicit false; hook must stay skipped
    res2 = runtime.run(RuntimeRequest(prompt="safe turn 2", session_id="ro-hook-session", metadata={"read_only": False}))
    assert res2.session.status == "completed"
    assert hook_marker.exists() is False
    skip2 = next(e for e in res2.events if e.event_type == "runtime.lifecycle_hook")
    assert skip2.payload["hook_status"] == "skipped"
    assert skip2.payload["hook_policy"]["read_only"] is True
    assert skip2.payload["hook_policy"]["outcome"] == "skipped"

    # Writable control: proving configured hook actually writes on a writable session
    res_control = runtime.run(RuntimeRequest(prompt="safe writable turn", session_id="writable-hook-session"))
    assert res_control.session.status == "completed"
    assert hook_marker.exists() is True


def test_default_policy_auto_approves_every_tier() -> None:
    read_tool = _read_only_tool()
    write_tool = _write_tool()
    default_policy = PermissionPolicy()

    assert default_policy.mode == DEFAULT_APPROVAL_MODE
    assert approval_decision(mode=DEFAULT_APPROVAL_MODE, operation_class="read") == "allow"
    assert approval_decision(mode=DEFAULT_APPROVAL_MODE, operation_class="write") == "allow"
    assert (
        resolve_permission(
            read_tool,
            _call("grep"),
            policy=default_policy,
            operation_class="read",
        ).decision
        == "allow"
    )
    assert (
        resolve_permission(
            write_tool,
            _call(),
            policy=default_policy,
            operation_class="write",
        ).decision
        == "allow"
    )


@pytest.mark.parametrize("mode", ["search", "preview"])
def test_ast_grep_non_replace_is_read_operation(mode: str) -> None:
    definition = ToolDefinition(name="ast_grep", description="AST search", effects=frozenset({ToolEffect.WRITE}))
    operation = operation_class_for_tool(
        "ast_grep",
        definition.effects,
        tool_instance=AstGrepTool(),
        arguments={"mode": mode},
    )
    assert operation == "read"


def test_apply_workspace_edit_paths_are_classified_by_runtime_permissions(tmp_path: Path) -> None:
    external = tmp_path.parent / "external-lsp-edit.txt"
    definition = ApplyWorkspaceEditTool.definition
    tool = ApplyWorkspaceEditTool()
    call = ToolCall(tool_name="apply_workspace_edit", arguments={"edits": [{"path": str(external)}]})
    resolver = RuntimePermissionContextResolver(workspace=tmp_path)

    scope, canonical_path, operation, candidates = resolver.permission_context_for_tool_call(
        tool=definition,
        tool_instance=tool,
        tool_call=call,
        patch_path_extractor=lambda _patch: (),
    )

    assert scope == "external"
    assert canonical_path == external.resolve().as_posix()
    assert operation == "write"
    assert candidates == (external.resolve().as_posix(),)


def test_ast_grep_replace_is_write_operation_and_denied_read_only(tmp_path: Path) -> None:
    definition = ToolDefinition(name="ast_grep", description="AST rewrite", effects=frozenset({ToolEffect.WRITE}))
    call = ToolCall(
        tool_name="ast_grep",
        arguments={"mode": "replace", "path": "sample.py", "apply": True},
    )
    resolver = RuntimePermissionContextResolver(workspace=tmp_path)
    _scope, _path, operation, _candidates = resolver.permission_context_for_tool_call(
        tool=definition,
        tool_instance=AstGrepTool(),
        tool_call=call,
        patch_path_extractor=lambda _patch: (),
    )
    assert operation == "write"
    outcome = resolve_permission(
        definition,
        call,
        policy=PermissionPolicy(),
        operation_class=operation,
        read_only=True,
    )
    assert outcome.decision == "deny"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval.policy_surface == "read_only"


class _StubTool:
    pass


class _StubResolver:
    """A resolver double pinned to two external paths: one allowed, one denied."""

    def permission_context_for_tool_call(
        self,
        *,
        tool: ToolDefinition,
        tool_instance: object,
        tool_call: ToolCall,
        patch_path_extractor: object,
    ) -> tuple[PathScope, str | None, OperationClass, tuple[str, ...]]:
        _ = tool, tool_instance, tool_call, patch_path_extractor
        return ("external", "/ext-a/x", "read", ("/ext-a/x", "/ext-b/y"))

    def normalized_permission_path_candidates(
        self,
        tool_call: ToolCall,
        external_paths: tuple[str, ...],
        *,
        patch_path_extractor: object,
        tool: ToolDefinition | None = None,
    ) -> tuple[str, ...]:
        _ = tool_call, external_paths, patch_path_extractor, tool
        return ()


def test_external_directory_policy_denies_across_multiple_external_paths() -> None:
    """A deny on any external path wins over allows on the other paths."""
    definition = ToolDefinition(name="read", description="read", input_schema={}, effects=frozenset({ToolEffect.READ}))
    engine = PermissionEngine(
        _context_resolver=_StubResolver(),  # type: ignore[arg-type]
        _permission_config=ExternalDirectoryPermissionConfig(),
    )

    evaluation = engine.evaluate(
        tool=definition,
        tool_instance=_StubTool(),  # type: ignore[arg-type]
        tool_call=ToolCall(tool_name="read", arguments={"path": "/ext-a/x"}),
        permission_rules=(),
        permission_config=ExternalDirectoryPermissionConfig(
            read=ExternalDirectoryPolicy(rules=(("/ext-b/*", "deny"), ("*", "allow"))),
        ),
    )

    assert evaluation.external_decision == "deny"
    assert evaluation.matched_rule == "/ext-b/*"
