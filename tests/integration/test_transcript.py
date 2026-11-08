"""Public transcript services and panels preserve committed history across restart."""

import json

from tests.unit.core.test_nested_tools import _call, _server, _turn
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.sdk.provider import ProviderMessage, ProviderTurnResult
from voidcode.sdk.transcript import TranscriptDispatch, TranscriptTool

PLUGIN = """
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolDefinition, ToolEffect, ToolSuccess, TextOutput

class Substitute:
    definition = ToolDefinition("arbitrary_lookup", "lookup", {"type": "object"}, effects=frozenset({ToolEffect.READ}))
    def invoke(self, call, *, context, services):
        return ToolSuccess(call.tool_name, TextOutput("lookup result"))

def create_plugin(config):
    return Plugin(tools=(Substitute(),))
"""


def _entries(core, session_id):
    cursor = 0
    entries = []
    empty_pages = 0
    while True:
        page = core.plugin_runtime.transcript_page(session_id, cursor, 1)
        entries.extend(page.entries)
        empty_pages += not page.entries
        cursor = page.next_sequence
        if not page.has_more:
            return entries, empty_pages


def test_public_transcript_survives_restart_without_clipping_messages(tmp_path):
    source = tmp_path / "probe"
    source.mkdir()
    (source / "__init__.py").write_text(PLUGIN)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    prompt = "完整用户正文" * 2_000
    answer = "完整答复" * 20_000 + "\n```python\nprint('complete')\n```"
    with _server([_turn(_call("lookup-1", "arbitrary_lookup")), _turn(output=answer)]) as server:
        plugins = {name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS}
        plugins["providers"] = {"enabled": True}
        plugins["probe"] = {"path": str(source)}
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/exact-model"},
                    "policy": {"approval_mode": "yolo"},
                    "providers": {"endpoint": {"api_key": "test", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                    "plugins": plugins,
                }
            )
        )
        with open_core(workspace, state_dir=state) as core:
            result = core.run(prompt)
            assert result.status == "completed", result.error
            entries, empty_pages = _entries(core, result.session_id)
            assert empty_pages > 0
            assert next(entry.content.content for entry in entries if isinstance(entry.content, ProviderMessage)) == prompt
            assert [entry.content.output for entry in entries if isinstance(entry.content, ProviderTurnResult)][-1] == answer
            tools = [entry.content for entry in entries if isinstance(entry.content, TranscriptTool)]
            assert len(tools) == 1 and tools[0].call.tool_name == "arbitrary_lookup"
            assert tools[0].result.output.text == "lookup result"
            dispatches = [entry for entry in entries if isinstance(entry.content, TranscriptDispatch)]
            assert len(dispatches) == 2
            assert all(entry.content.provider_name == "endpoint" and entry.content.model_name == "exact-model" for entry in dispatches)
            expected = entries
        with open_core(workspace, state_dir=state) as core:
            restored, _ = _entries(core, result.session_id)
            assert restored == expected
