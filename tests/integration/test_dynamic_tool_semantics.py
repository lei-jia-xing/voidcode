"""Failed mixed-mode calls keep invocation semantics, including pre-execution refusals."""

import json
from pathlib import Path

import pytest

from tests.integration.test_sdk_facts import history
from tests.unit.core.test_nested_tools import _call, _server, _turn
from voidcode.core.bootstrap import open_core
from voidcode.sdk.hooks import InvocationFinishedFact
from voidcode.sdk.tools import ToolSemantic

PLUGIN = """
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolDefinition, ToolEffect, ToolSemantic, ToolSuccess, ToolFailure, TextOutput

class Multi:
    definition = ToolDefinition("custom_multi", "mixed operations",
        input_schema={"type": "object", "properties": {"operation": {"enum": ["search", "preview", "replace"]}},
                      "required": ["operation"], "additionalProperties": False},
        effects=frozenset({ToolEffect.WRITE}),
        semantic_rules={ToolSemantic.RESOURCE_EDIT: {"properties": {"operation": {"const": "replace"}}, "required": ["operation"]}})
    def invoke(self, call, *, context, services):
        if call.arguments["operation"] == "replace":
            return ToolFailure(call.tool_name, "replacement refused")
        return ToolSuccess(call.tool_name, TextOutput("read result"))

def create_plugin(config):
    return Plugin(tools=(Multi(),))
"""


@pytest.mark.parametrize("readonly", [False, True])
def test_failed_edit_classification_and_metrics_survive_catalog_changes(tmp_path, readonly):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__init__.py").write_text(PLUGIN)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugins = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins"
    with _server(
        [
            _turn(*(_call(mode, "custom_multi", operation=mode) for mode in ("search", "preview", "replace"))),
            _turn(output="done"),
        ]
    ) as server:
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/exact-model"},
                    "policy": {"approval_mode": "yolo", "read_only": readonly},
                    "providers": {"endpoint": {"api_key": "test", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                    "plugins": {"multi": {"path": str(source)}, "effectiveness": {"path": str(plugins / "effectiveness")}},
                }
            )
        )
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            result = core.run("exercise mixed operations")
            assert result.status == "completed", result.error
            facts = tuple(fact for fact in history(core, result.session_id) if isinstance(fact, InvocationFinishedFact))
            assert [fact.semantics for fact in facts] == [(), (), (ToolSemantic.RESOURCE_EDIT,)]
            assert facts[-1].status == "error" and not facts[-1].changes
            intents = [event for event in core.store.events(result.session_id) if event.event_type == "kernel.tool_intent"]
            assert len(intents) == (0 if readonly else 3)
            if intents:
                assert [event.payload["semantics"] for event in intents] == [(), (), ("resource.edit",)]
            report = core.invoke_command(result.session_id, "effectiveness", {}, expected_revision=core.revision_id)
            assert report.status == "ok", report.error
            assert report.data["models"][0]["edit_calls"] == 1
            assert report.data["models"][0]["edit_errors"] == 1
        (source / "__init__.py").write_text(PLUGIN.replace('"const": "replace"', '"const": "search"'))
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            restored = tuple(fact for fact in history(core, result.session_id) if isinstance(fact, InvocationFinishedFact))
            assert restored == facts
