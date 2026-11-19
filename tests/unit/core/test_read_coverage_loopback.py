"""Read observations retain bounds across real provider calls and durable facts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.s5_plugin import PROBE_TOOL
from tests.unit.core.test_nested_tools import _call, _configure, _open, _server, _turn
from voidcode.core.coverage import read_artifact_content
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.hooks import InvocationFinishedFact
from voidcode.sdk.tools import TOOL_OUTPUT_BYTES


@pytest.mark.parametrize("flags", [{"partial": True}, {"truncated": True}, {"clipped": True}, {"output_bytes": 52 * 1024}])
def test_read_observations_preserve_output_bounds_in_public_facts(tmp_path: Path, flags: dict[str, object]) -> None:
    with _server(
        [
            _turn(
                _call("bounded", PROBE_TOOL, action="observe", path="document.txt", **flags),
            ),
            _turn(output="done"),
        ]
    ) as server:
        workspace = _configure(tmp_path, server)
        (workspace / "document.txt").write_text("visible\n", encoding="utf-8")
        with _open(tmp_path) as core:
            result = core.run("Check incomplete output.")
            assert result.status == "completed", result.error
            facts = core.plugin_runtime.fact_page(result.session_id, 0, 200).facts
            observed = next(fact for fact in facts if isinstance(fact, InvocationFinishedFact) and fact.tool_call_id == "bounded")
            assert len(observed.reads) == 1
            assert observed.reads[0].content_hash
            if "partial" in flags:
                assert observed.partial is True
            if "truncated" in flags:
                assert observed.truncated is True
            if "clipped" in flags:
                assert observed.reads[0].clipped is True
            if "output_bytes" in flags:
                assert observed.truncated is True and observed.reads[0].clipped is True
                messages = server.requests[1]["messages"]
                projection = next(message["content"] for message in messages if message.get("role") == "tool")
                document = json.loads(projection)
                assert len(projection.encode()) <= TOOL_OUTPUT_BYTES
                assert document["partial"] and len(document["text"].encode()) < flags["output_bytes"]
                completed = next(event for event in core.events_after(result.session_id) if event.event_type == "core.tool_completed")
                reference = decode_tool_result(completed.payload["result"]).result_reference
                assert reference is not None and document["result_uri"] == reference.uri
                original = json.loads(read_artifact_content(reference, core._artifact_page))
                assert len(original["output"]["text"].encode()) == flags["output_bytes"]
