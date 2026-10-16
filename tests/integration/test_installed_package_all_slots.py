from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from voidcode.runtime.composition import (
    ComponentSelection,
)
from voidcode.runtime.config.loading import RuntimeAgentConfig, RuntimeConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import VoidCodeRuntime


def _build_and_install_wheel(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "wheel_build"
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = dist_dir / "all_slots_pkg-1.0.0-py3-none-any.whl"
    dist_info = "all_slots_pkg-1.0.0.dist-info"

    components_code = """from __future__ import annotations
from typing import Any, cast
from collections.abc import Iterator

from voidcode.agent import AgentManifest, AgentPromptMaterialization
from voidcode.core.tool_context import ToolContext
from voidcode.core.transcript import output_text
from voidcode.hook.before_agent_start import (
    BeforeAgentStartBinding,
    BeforeAgentStartDeclaration,
    BeforeAgentStartRequest,
    BeforeAgentStartTransform,
)
from voidcode.hook.typed import (
    ToolInputDecision,
    ToolInputEvent,
    ToolInputHandlerBinding,
    ToolInputHandlerDeclaration,
    RewriteDecision,
)
from voidcode.provider.models import ProviderDescriptor
from voidcode.provider.protocol import (
    ModelTurnProvider,
    ProviderStreamEvent,
    ProviderTurnRequest,
    ProviderTurnResult,
    TurnProvider,
)
from voidcode.runtime.composition import ValidatedConfig
from voidcode.runtime.context.transforms import (
    RuntimeContextTransformDeclaration,
    RuntimeContextTransformInjection,
    RuntimeContextTransformProvider,
    RuntimeContextTransformRequest,
    RuntimeContextTransformResult,
)
from voidcode.tools.contracts import (
    TextOutput,
    Tool,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolSuccess,
)

def validate_provider(config: object) -> ValidatedConfig:
    return ValidatedConfig(
        value=ProviderDescriptor(provider_name="pkg_provider"),
        snapshot={"name": "pkg_provider"},
    )

class PkgTurnProvider:
    name = "pkg_provider"

    def __init__(self) -> None:
        self.turn_count = 0

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        tool_names = tuple(t.name for t in request.available_tools)
        if "pkg_tool" not in tool_names:
            return ProviderTurnResult(output="ERROR: pkg_tool not in available tools", done_reason="stop", finish_reason_reported=True)

        context_has_rule = any(
            "[RULE: package_context_active]" in (segment.content or "")
            for segment in request.assembled_context.segments
        )
        if not context_has_rule:
            return ProviderTurnResult(output="ERROR: context transform not observed", done_reason="stop", finish_reason_reported=True)

        agent_directive_active = any(
            "pkg_agent_directive_active" in (segment.content or "")
            for segment in request.assembled_context.segments
        )
        if not agent_directive_active:
            return ProviderTurnResult(output="ERROR: package agent directive not in prompt context", done_reason="stop", finish_reason_reported=True)

        self.turn_count += 1
        if self.turn_count == 1:
            return ProviderTurnResult(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_pkg_1",
                        tool_name="pkg_tool",
                        arguments={"message": "input_payload"},
                    ),
                ),
                done_reason="tool_calls",
                finish_reason_reported=True,
            )

        tool_result = request.assembled_context.tool_results[0]
        tool_output = output_text(tool_result.output)
        return ProviderTurnResult(
            output=f"FINAL_RESULT: context_and_agent_verified | {tool_output}",
            done_reason="stop",
            finish_reason_reported=True,
        )
    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]:
        turn = self.propose_turn(request)
        if turn.tool_calls:
            call = turn.tool_calls[0]
            yield ProviderStreamEvent(
                kind="tool_call_start",
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
            )
            yield ProviderStreamEvent(
                kind="tool_call_end",
                tool_call_id=call.tool_call_id,
                tool_name=call.tool_name,
                parsed_arguments=dict(call.arguments),
            )
            yield ProviderStreamEvent(kind="done", done_reason="tool_calls")
        else:
            yield ProviderStreamEvent(
                kind="content",
                text=turn.output,
            )
            yield ProviderStreamEvent(kind="done", done_reason="stop")

class PkgModelTurnProvider:
    name = "pkg_provider"

    def __init__(self) -> None:
        self._provider = PkgTurnProvider()

    def turn_provider(self) -> TurnProvider:
        return self._provider

def create_provider(value: object, context: object) -> object:
    return PkgModelTurnProvider()

def validate_tool(config: object) -> ValidatedConfig:
    definition = ToolDefinition(
        name="pkg_tool",
        description="Test package tool",
        input_schema={"type": "object", "properties": {"message": {"type": "string"}}},
        effects=frozenset({ToolEffect.READ}),
    )
    return ValidatedConfig(
        value=definition,
        snapshot={"name": "pkg_tool"},
    )

class PkgTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        msg = call.arguments.get("message")
        return ToolSuccess(
            tool_name="pkg_tool",
            output=TextOutput(f"TOOL_EXECUTED: {msg}"),
        )

def create_tool(value: object, context: object) -> object:
    return PkgTool(cast(ToolDefinition, value))

def validate_agent(config: object) -> ValidatedConfig:
    manifest = AgentManifest(
        id="pkg_agent",
        name="Package Agent",
        description="Agent from package",
        mode="primary",
        top_level_selectable=True,
        model_preference="pkg_provider/demo_model",
        tool_allowlist=("pkg_tool",),
        prompt_profile="pkg_agent",
        source_scope="package",
        source_id="package:all_slots_pkg:pkg_agent",
        prompt_materialization=AgentPromptMaterialization(
            profile="pkg_agent",
            version=1,
            source="custom_markdown",
            format="markdown",
            body="pkg_agent_directive_active",
            source_scope="package",
            source_id="package:all_slots_pkg:pkg_agent",
        ),
    )
    return ValidatedConfig(
        value=manifest,
        snapshot={"id": "pkg_agent", "name": "Package Agent"},
    )

def create_agent(value: object, context: object) -> object:
    return value

def validate_context(config: object) -> ValidatedConfig:
    decl = RuntimeContextTransformDeclaration(
        provider_id="pkg_context",
        provider_version="1",
        scope="provider_context",
        priority=10,
        failure_policy="warn",
    )
    return ValidatedConfig(
        value=decl,
        snapshot={"name": "pkg_context"},
    )

class PkgContextTransformProvider:
    provider_id = "pkg_context"
    provider_version = "1"
    scope = "provider_context"
    priority = 10
    failure_policy = "warn"

    def build_result(self, request: RuntimeContextTransformRequest) -> RuntimeContextTransformResult:
        return RuntimeContextTransformResult(
            injections=(
                RuntimeContextTransformInjection(
                    role="system",
                    content="[RULE: package_context_active]",
                    metadata={"source": "pkg_context"},
                ),
            )
        )

def create_context(value: object, context: object) -> object:
    return PkgContextTransformProvider()

def validate_typed_input(config: object) -> ValidatedConfig:
    decl = ToolInputHandlerDeclaration(
        name="pkg_input",
        version="1",
        priority=1,
    )
    return ValidatedConfig(
        value=decl,
        snapshot={"name": "pkg_input"},
    )

class PkgInputHandler:
    def __call__(self, event: ToolInputEvent) -> ToolInputDecision:
        orig = event.tool_call.arguments.get("message", "")
        return RewriteDecision(arguments={"message": f"{orig} -> rewritten_by_typed_input"})

def create_typed_input(value: object, context: object) -> object:
    decl = cast(ToolInputHandlerDeclaration, value)
    return ToolInputHandlerBinding(declaration=decl, handler=PkgInputHandler())
def validate_before_agent_start(config: object) -> ValidatedConfig:
    declaration = BeforeAgentStartDeclaration(name="pkg_prepare", version="1", priority=5)
    return ValidatedConfig(value=declaration, snapshot={"name": declaration.name})

class PkgBeforeAgentStart:
    def __call__(self, request: BeforeAgentStartRequest) -> BeforeAgentStartTransform:
        return BeforeAgentStartTransform(prompt=f"package prepared: {request.effective_prompt}")

def create_before_agent_start(value: object, context: object) -> object:
    declaration = cast(BeforeAgentStartDeclaration, value)
    return BeforeAgentStartBinding(declaration=declaration, handler=PkgBeforeAgentStart())
"""

    manifest_dict = {
        "schema_version": 1,
        "files": [
            "all_slots_pkg/__init__.py",
            "all_slots_pkg/components.py",
        ],
        "components": [
            {
                "slot": "provider",
                "name": "pkg_provider",
                "factory": "all_slots_pkg.components:create_provider",
                "validate_config": "all_slots_pkg.components:validate_provider",
            },
            {
                "slot": "tool",
                "name": "pkg_tool",
                "factory": "all_slots_pkg.components:create_tool",
                "validate_config": "all_slots_pkg.components:validate_tool",
            },
            {
                "slot": "agent",
                "name": "pkg_agent",
                "factory": "all_slots_pkg.components:create_agent",
                "validate_config": "all_slots_pkg.components:validate_agent",
            },
            {
                "slot": "context",
                "name": "pkg_context",
                "factory": "all_slots_pkg.components:create_context",
                "validate_config": "all_slots_pkg.components:validate_context",
            },
            {
                "slot": "typed-input",
                "name": "pkg_input",
                "factory": "all_slots_pkg.components:create_typed_input",
                "validate_config": "all_slots_pkg.components:validate_typed_input",
            },
            {
                "slot": "before-agent-start",
                "name": "pkg_prepare",
                "factory": "all_slots_pkg.components:create_before_agent_start",
                "validate_config": "all_slots_pkg.components:validate_before_agent_start",
            },
        ],
        "phases": [],
    }

    contents = {
        "all_slots_pkg/__init__.py": "",
        "all_slots_pkg/components.py": components_code,
        f"{dist_info}/METADATA": "Metadata-Version: 2.1\nName: all-slots-pkg\nVersion: 1.0.0\n",
        f"{dist_info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist_info}/voidcode-components.json": json.dumps(manifest_dict),
    }

    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
        record_content = "".join(f"{name},,\n" for name in contents) + f"{dist_info}/RECORD,,\n"
        archive.writestr(f"{dist_info}/RECORD", record_content)

    site_dir = tmp_path / "site_packages"
    site_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "pip", "install", "--no-deps", "--target", str(site_dir), str(wheel_path)],
        check=True,
        capture_output=True,
    )
    return site_dir


@pytest.mark.parametrize("stream", [False, True])
def test_installed_package_all_six_slots_end_to_end(tmp_path: Path, stream: bool) -> None:
    site_dir = _build_and_install_wheel(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(site_dir))
    try:
        with VoidCodeRuntime(
            workspace=workspace,
            config=RuntimeConfig(
                model="pkg_provider/demo_model",
                agent=RuntimeAgentConfig(preset="pkg_agent"),
                components=(
                    ComponentSelection(distribution="all-slots-pkg", slot="provider", name="pkg_provider", configuration={}, provenance=("test",)),
                    ComponentSelection(distribution="all-slots-pkg", slot="tool", name="pkg_tool", configuration={}, provenance=("test",)),
                    ComponentSelection(distribution="all-slots-pkg", slot="agent", name="pkg_agent", configuration={}, provenance=("test",)),
                    ComponentSelection(distribution="all-slots-pkg", slot="context", name="pkg_context", configuration={}, provenance=("test",)),
                    ComponentSelection(distribution="all-slots-pkg", slot="typed-input", name="pkg_input", configuration={}, provenance=("test",)),
                    ComponentSelection(
                        distribution="all-slots-pkg",
                        slot="before-agent-start",
                        name="pkg_prepare",
                        configuration={},
                        provenance=("test",),
                    ),
                ),
            ),
        ) as runtime:
            request = RuntimeRequest(
                prompt="Run full all-slot verification",
                session_id=f"all-slots-session-{stream}",
                metadata={"provider_stream": stream},
            )

            chunks = list(runtime.run_with_persistence(request))
            response = runtime.session_result(session_id=f"all-slots-session-{stream}")
            events = runtime.replay_session(session_id=f"all-slots-session-{stream}").events

            assert response.session.status == "completed"
            trace = next(event for event in events if event.event_type == "runtime.before_agent_start")
            assert trace.payload["status"] == "ready"
            assert trace.payload["changed"] is True
            assert trace.payload["handlers"][0]["name"] == "pkg_prepare"
            assert "prompt" not in trace.payload
            preparation = response.session.metadata["before_agent_start"]
            assert preparation["effective_prompt"] == "package prepared: Run full all-slot verification"

            expected_final = "FINAL_RESULT: context_and_agent_verified | TOOL_EXECUTED: input_payload -> rewritten_by_typed_input"
            assert response.output == expected_final

            # In streaming path, verify chunks also delivered output
            if stream:
                outputs = [c.output for c in chunks if c.output is not None]
                assert any(expected_final in out for out in outputs)

    finally:
        sys.path.remove(str(site_dir))
        for key in list(sys.modules):
            if key.startswith("all_slots_pkg"):
                del sys.modules[key]
