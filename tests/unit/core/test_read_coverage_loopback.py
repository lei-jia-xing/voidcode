"""Read grants cross the real SDK wire and rebuild from durable visible results."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.fixtures.s5_plugin import PROBE_TOOL
from tests.unit.core.test_nested_tools import _call, _configure, _open, _server, _turn
from voidcode.core.engine import Core
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.hooks import InvocationFinishedFact
from voidcode.sdk.tools import TextOutput, ToolSuccess


def _context_body(core: Core, session_id: str) -> dict[str, object]:
    for event in reversed(core.store.events(session_id)):
        if event.event_type != "kernel.tool_completed" or event.payload["call"]["arguments"].get("action") != "coverage_context":
            continue
        result = decode_tool_result(event.payload["result"])
        assert isinstance(result, ToolSuccess) and isinstance(result.output, TextOutput)
        return json.loads(result.output.text)
    raise AssertionError("context result was not committed")


def test_coverage_rebuilds_after_restart_without_cross_version_or_session_unions(tmp_path: Path) -> None:
    with _server(
        [
            _turn(
                _call("read-v1", PROBE_TOOL, action="observe", path="document.txt"),
                _call("context-v1", PROBE_TOOL, action="coverage_context", path="document.txt"),
            ),
            _turn(output="first"),
            _turn(
                _call("read-v2", PROBE_TOOL, action="observe", path="document.txt", start=1, end=1),
                _call("context-v2", PROBE_TOOL, action="coverage_context", path="document.txt"),
            ),
            _turn(output="second"),
            _turn(_call("foreign", PROBE_TOOL, action="coverage_context", path="document.txt")),
            _turn(output="third"),
        ]
    ) as server:
        workspace = _configure(tmp_path, server)
        path = workspace / "document.txt"
        first_text = "one\ntwo\nthree\n"
        first_hash = hashlib.sha256(first_text.encode()).hexdigest()
        path.write_text(first_text, encoding="utf-8")
        with _open(tmp_path) as core:
            first = core.run("Read the first version.", session_id="coverage-owner")
            assert first.status == "completed", first.error
            context = _context_body(core, first.session_id)
            assert context["read_paths"] == [str(path)]
            assert context["read_lines"] == [{"path": str(path), "content_hash": first_hash, "lines": [1, 2, 3]}]
            assert context["read_whole_files"] == [[str(path), first_hash]]
            assert context["read_hash"] == first_hash
        second_text = "changed\nsecond\nthird\n"
        second_hash = hashlib.sha256(second_text.encode()).hexdigest()
        path.write_text(second_text, encoding="utf-8")
        with _open(tmp_path) as core:
            second = core.run("Read only the new first line.", session_id=first.session_id)
            assert second.status == "completed", second.error
            context = _context_body(core, second.session_id)
            by_hash = {entry["content_hash"]: entry["lines"] for entry in context["read_lines"]}
            assert by_hash == {first_hash: [1, 2, 3], second_hash: [1]}
            assert context["read_whole_files"] == [[str(path), first_hash]]
            assert context["read_hash"] is None
            foreign = core.run("Inspect another session's context.", session_id="coverage-foreign")
            assert foreign.status == "completed", foreign.error
            context = _context_body(core, foreign.session_id)
            assert context["read_paths"] == [] and context["read_lines"] == [] and context["read_whole_files"] == []
            assert context["read_hash"] is None


@pytest.mark.parametrize("flags", [{"partial": True}, {"truncated": True}, {"clipped": True}, {"output_bytes": 52 * 1024}])
def test_incomplete_or_model_clipped_result_grants_no_coverage(tmp_path: Path, flags: dict[str, object]) -> None:
    with _server(
        [
            _turn(
                _call("bounded", PROBE_TOOL, action="observe", path="document.txt", **flags),
                _call("context", PROBE_TOOL, action="coverage_context", path="document.txt"),
            ),
            _turn(output="done"),
        ]
    ) as server:
        workspace = _configure(tmp_path, server)
        (workspace / "document.txt").write_text("visible\n", encoding="utf-8")
        with _open(tmp_path) as core:
            result = core.run("Check incomplete output.")
            assert result.status == "completed", result.error
            context = _context_body(core, result.session_id)
            assert context["read_paths"] == [] and context["read_lines"] == [] and context["read_whole_files"] == []
            assert context["read_hash"] is None
            facts = core.plugin_runtime.fact_page(result.session_id, 0, 200).facts
            observed = next(fact for fact in facts if isinstance(fact, InvocationFinishedFact) and fact.tool_call_id == "bounded")
            if "output_bytes" in flags:
                assert observed.truncated is True and observed.reads[0].clipped is True
                messages = server.requests[1]["messages"]
                assert any("model projection truncated" in str(message.get("content")) for message in messages if message.get("role") == "tool")
