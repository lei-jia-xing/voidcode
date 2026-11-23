"""Post-tool notes append to a result without flattening its typed output."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from tests.fixtures.core_factory import loopback_server, open_core_for_test, write_loopback_config
from tests.provider_loopback import openai_response
from voidcode.core.engine.governance import ToolGovernance
from voidcode.core.execution import _Cursor, _Execution
from voidcode.core.storage.store import SessionRecord, SessionStatus
from voidcode.sdk.codec import decode_tool_call, decode_tool_result
from voidcode.sdk.services import ArtifactReference
from voidcode.sdk.tools import (
    Change,
    CompositeOutput,
    EmptyOutput,
    OpaqueToolBody,
    ReadObservation,
    TablePart,
    TextOutput,
    TextPart,
    ToolCall,
    ToolSuccess,
)

PLUGIN_ID = "hook-output-fixture"
NOTE = "post hook note"

PLUGIN_SOURCE = '''"""Fixture: MCP, web-style, and text tool results with one post-tool note."""

from __future__ import annotations

from hashlib import sha256

from voidcode.plugins.mcp.mcp_tool import McpTool
from voidcode.plugins.mcp.types import McpToolCallResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import (
    Change,
    CompositeOutput,
    EmptyOutput,
    OpaqueToolBody,
    OutputBounds,
    ReadObservation,
    TablePart,
    TextOutput,
    TextPart,
    ToolDefinition,
    ToolEffect,
    ToolSuccess,
)

NOTE = "post hook note"


class _Manager:
    def call_tool(self, **kwargs):
        _ = kwargs
        return McpToolCallResult(content=[{"type": "text", "text": "remote mcp text"}])

    def descriptor_for(self, **kwargs):
        _ = kwargs
        return None


class _Shapes:
    definition = ToolDefinition(
        name="shape_probe",
        description="Return the result shape named by action.",
        input_schema={
            "type": "object",
            "properties": {"action": {"enum": ["web", "text"]}},
            "required": ["action"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
    )

    def invoke(self, call, *, context, services):
        action = str(call.arguments["action"])
        reference = services.spill_artifact(b"typed body", media_type="text/plain; charset=utf-8")
        coverage = (ReadObservation("note.txt", sha256(b"note").hexdigest(), (1,), 1, True, False),)
        changes = (Change("note.txt", "update"),)
        if action == "web":
            output = EmptyOutput(
                "Fetched https://example.test/",
                bounds=OutputBounds(reference=reference, source="https://example.test/"),
            )
            body = OpaqueToolBody({"url": "https://example.test/", "bytes": 10})
        else:
            output = TextOutput(
                "plain text result",
                presentation="text presentation",
                bounds=OutputBounds(reference=reference),
            )
            body = OpaqueToolBody({"kind": "text"})
        return ToolSuccess(
            call.tool_name,
            output,
            body,
            coverage,
            changes,
            steering_context=f"steer-{action}",
            result_reference=reference,
        )


def _post(outcome, context, services):
    _ = outcome, context, services
    return NOTE


def create_plugin(config=None):
    _ = config
    return Plugin(
        tools=(McpTool(manager=_Manager()), _Shapes()),
        post_tool=_post,
        post_tool_match=("*",),
    )
'''


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "output note reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _decode_completed(core, session_id: str) -> dict[str, object]:
    results: dict[str, object] = {}
    for event in core.events_after(session_id):
        if event.event_type != "core.tool_completed":
            continue
        call = decode_tool_call(event.payload["call"])
        results[str(call.tool_call_id)] = decode_tool_result(event.payload["result"])
    return results


def test_post_hook_note_preserves_mcp_web_and_text_output(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    source.mkdir()
    (source / "__init__.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    replies = [
        _turn(
            _call("mcp-1", "mcp", server="server", tool="remote"),
            _call("web-1", "shape_probe", action="web"),
            _call("text-1", "shape_probe", action="text"),
        ),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        workspace = tmp_path / "workspace"
        write_loopback_config(workspace, server, source, plugin_id=PLUGIN_ID)
        config_path = workspace / "voidcode.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["plugins"]["mcp"] = {"enabled": False}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        core = open_core_for_test(tmp_path)
        core.create_session("sess-output")
        result = core.run("run all shapes", session_id="sess-output")
        assert result.status == "completed", result.error

        results = _decode_completed(core, "sess-output")
        mcp = results["mcp-1"]
        web = results["web-1"]
        text = results["text-1"]
        assert isinstance(mcp, ToolSuccess) and isinstance(web, ToolSuccess) and isinstance(text, ToolSuccess)

        # MCP publishes CompositeOutput(presentation=None); the note is an extra TextPart,
        # never a replacement of the full-result reference, remote text, or table.
        assert isinstance(mcp.output, CompositeOutput)
        assert mcp.output.presentation is None
        assert isinstance(mcp.output.parts[0], TextPart)
        assert mcp.output.parts[0].text.startswith("Full MCP result JSON: artifact://")
        assert any(isinstance(part, TextPart) and part.text == "remote mcp text" for part in mcp.output.parts)
        assert mcp.output.parts[-1] == TextPart(NOTE)
        assert mcp.output.bounds.reference is not None
        assert mcp.output.bounds.source == "mcp:server/remote"
        assert isinstance(mcp.body, OpaqueToolBody) and mcp.body.structured_content["tool"] == "remote"

        # Web keeps EmptyOutput and its result artifact reference; the note extends the
        # presentation instead of turning the result into loose text.
        assert isinstance(web.output, EmptyOutput)
        assert web.output.presentation == f"Fetched https://example.test/\n\n{NOTE}"
        assert web.output.bounds.reference is not None
        assert web.output.bounds.source == "https://example.test/"
        assert isinstance(web.body, OpaqueToolBody) and web.body.structured_content["url"] == "https://example.test/"
        assert web.steering_context == "steer-web"
        assert web.coverage and web.changes

        # Text results are the regression case: content and presentation survive.
        assert isinstance(text.output, TextOutput)
        assert text.output.text == f"plain text result\n\n{NOTE}"
        assert text.output.presentation == "text presentation"
        assert text.output.bounds.reference is not None
        assert text.steering_context == "steer-text"
        assert text.coverage and text.changes

        # All three notes are visible to the model on the very next dispatch.
        tool_messages = [
            str(message.get("content", ""))
            for message in server.requests[1]["messages"]
            if isinstance(message, dict) and message.get("role") == "tool"
        ]
        assert len(tool_messages) == 3
        assert all(NOTE in message for message in tool_messages), tool_messages


class _Runtime:
    post_tool_hooks = (("fixture", ("*",), False),)

    def post_tool(self, owner, **kwargs):
        _ = owner, kwargs
        return NOTE


class _Snapshot:
    plugin_runtime = _Runtime()


class _Host:
    policy_read_only = False
    store = SimpleNamespace(session_overlays=lambda session_id: ())


def test_governance_preserves_every_success_field_and_reference(tmp_path: Path) -> None:
    reference = ArtifactReference(sha256(b"reference").hexdigest(), 9, "text/plain")
    coverage = (ReadObservation("note.txt", sha256(b"note").hexdigest(), (1,), 1, True, False),)
    changes = (Change("note.txt", "update"),)
    execution = _Execution(
        SessionRecord("sess-unit", str(tmp_path), SessionStatus.RUNNING, "rev", "run", 1, 0),
        _Cursor(),
        [],
        "run",
    )
    call = ToolCall("fixture_tool", {}, "call-1")
    for output in (
        TextOutput("text", "presentation"),
        EmptyOutput("presentation"),
        CompositeOutput((TextPart("part"), TablePart(columns=("a",), rows=(("1",),))), presentation=None),
    ):
        original = ToolSuccess(
            "fixture_tool",
            output,
            OpaqueToolBody({"k": "v"}),
            coverage,
            changes,
            steering_context="steer",
            result_reference=reference,
        )
        updated = ToolGovernance(_Host()).post_tool_hooks(execution, _Snapshot(), call, original)
        assert type(updated.output) is type(output)
        assert updated.output.bounds == output.bounds
        assert updated.body is original.body
        assert updated.coverage == coverage and updated.changes == changes
        assert updated.steering_context == "steer"
        assert updated.result_reference == reference
