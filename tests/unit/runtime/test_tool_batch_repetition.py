from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.turn_producers import model_turn_fact
from voidcode.core.engine import TurnBatch
from voidcode.core.turns import FinalTurn, ReportedCall, ToolTurn, TurnRequest
from voidcode.hook.config import RuntimeHooksConfig
from voidcode.runtime.config.loading import RuntimeConfig, RuntimeMcpConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.permissions.policy import PermissionPolicy
from voidcode.runtime.run_loop import ToolBatchRepetition
from voidcode.runtime.service import ToolRegistry, VoidCodeRuntime
from voidcode.tools.contracts import TextOutput, ToolCall, ToolFailure, ToolSuccess
from voidcode.tools.read import ReadTool


class _ReadSequence:
    def __init__(self, batches: tuple[tuple[ToolCall, ...], ...], *, changed_file: Path | None = None) -> None:
        self.batches = batches
        self.changed_file = changed_file

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> FinalTurn | ToolTurn:
        _ = session
        fact = model_turn_fact(request)
        if self.changed_file is not None and len(tool_results) == 1:
            self.changed_file.write_text("changed", encoding="utf-8")
        offset = 0
        for batch in self.batches:
            if len(tool_results) == offset:
                return ToolTurn(calls=batch, facts=(fact,))
            offset += len(batch)
        return FinalTurn(output="done", facts=(fact,))


def _read(path: str) -> ToolCall:
    return ToolCall("read", {"path": path})


def _runtime(workspace: Path, producer: _ReadSequence, *, hooks: RuntimeHooksConfig | None = None) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=workspace,
        tool_registry=ToolRegistry.from_tools((ReadTool(),)),
        turn_producer=producer,
        config=RuntimeConfig(
            approval_mode="yolo",
            mcp=RuntimeMcpConfig(enabled=False),
            hooks=hooks,
        ),
        permission_policy=PermissionPolicy(mode="yolo"),
    )


def _run_reads(
    workspace: Path,
    producer: _ReadSequence,
    *,
    session_id: str,
    hooks: RuntimeHooksConfig | None = None,
):
    with _runtime(workspace, producer, hooks=hooks) as runtime:
        return runtime.run(RuntimeRequest(prompt="read sequence", session_id=session_id))


def _repeat_events(response: object) -> list[object]:
    return [event for event in response.events if event.event_type == "runtime.tool_batch_repeated"]


def _read_count(response: object) -> int:
    return sum(event.event_type == "runtime.tool_completed" for event in response.events)


def test_runtime_repeat_smoke_reports_real_read_counts_and_hook_order(tmp_path: Path) -> None:
    for index in range(26):
        (tmp_path / f"unique-{index}.txt").write_text(f"unique-{index}", encoding="utf-8")
    unique = _run_reads(
        tmp_path,
        _ReadSequence(tuple((_read(f"unique-{index}.txt"),) for index in range(26))),
        session_id="unique-26",
    )
    unique_repeats = _repeat_events(unique)
    assert _read_count(unique) == 26
    assert unique_repeats == []

    (tmp_path / "same.txt").write_text("stable", encoding="utf-8")
    identical = _run_reads(
        tmp_path,
        _ReadSequence(((_read("same.txt"),), (_read("same.txt"),))),
        session_id="identical",
    )
    identical_repeats = _repeat_events(identical)
    assert _read_count(identical) == 2
    assert len(identical_repeats) == 1
    assert identical_repeats[0].payload == {"repeat_count": 2, "tool_names": ["read"], "batch_size": 1}

    (tmp_path / "changing.txt").write_text("first", encoding="utf-8")
    changed = _run_reads(
        tmp_path,
        _ReadSequence(((_read("changing.txt"),), (_read("changing.txt"),)), changed_file=tmp_path / "changing.txt"),
        session_id="changed-result",
    )
    assert _read_count(changed) == 2
    assert _repeat_events(changed) == []

    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    reordered = _run_reads(
        tmp_path,
        _ReadSequence(
            (
                (_read("a.txt"), _read("b.txt")),
                (_read("b.txt"), _read("a.txt")),
            )
        ),
        session_id="reordered",
    )
    assert _read_count(reordered) == 4
    assert _repeat_events(reordered) == []

    class _HistoryProducer:
        def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object) -> FinalTurn | ToolTurn:
            fact = model_turn_fact(request)
            _ = session
            if request.prompt == "seed history" and len(tool_results) < 2:
                return ToolTurn(calls=(_read("same.txt"),), facts=(fact,))
            if request.prompt == "one new read" and request.run_step == 1:
                return ToolTurn(calls=(_read("same.txt"),), facts=(fact,))
            return FinalTurn(output="done", facts=(fact,))

    with _runtime(tmp_path, _HistoryProducer()) as history_runtime:
        history_runtime.run(RuntimeRequest(prompt="seed history", session_id="history"))
        second_run = history_runtime.run(RuntimeRequest(prompt="one new read", session_id="history"))

    hook_payload_path = tmp_path / "hook-payload.json"
    script = (
        "import json, os, pathlib; "
        f"pathlib.Path({str(hook_payload_path)!r}).write_text(os.environ['VOIDCODE_HOOK_PAYLOAD_JSON']); "
        "print(json.dumps({'action': 'cancel', 'diagnostic': 'confirmed'}))"
    )
    cancelled = _run_reads(
        tmp_path,
        _ReadSequence(((_read("same.txt"),), (_read("same.txt"),))),
        session_id="cancel-hook",
        hooks=RuntimeHooksConfig(
            enabled=True,
            on_tool_batch_repeated=((sys.executable, "-c", script),),
        ),
    )
    hook_payload = json.loads(hook_payload_path.read_text(encoding="utf-8"))
    fact_events = _repeat_events(cancelled)
    hook_events = [event for event in cancelled.events if event.event_type == "runtime.lifecycle_hook"]
    assert len(fact_events) == len(hook_events) == 1
    fact = fact_events[0]
    hook_event = hook_events[0]
    assert hook_event.payload["surface"] == "tool_batch_repeated"
    assert hook_payload == fact.payload == {"repeat_count": 2, "tool_names": ["read"], "batch_size": 1}
    assert not ({"arguments", "output", "result"} & hook_event.payload.keys())
    assert fact.sequence < hook_event.sequence

    print(
        "REPEAT_SMOKE "
        + json.dumps(
            {
                "unique_26_reads": {"completed_reads": _read_count(unique), "repeat_facts": len(unique_repeats)},
                "identical": {"completed_reads": _read_count(identical), "repeat_facts": len(identical_repeats)},
                "changed_result": {"completed_reads": _read_count(changed), "repeat_facts": len(_repeat_events(changed))},
                "reordered_multi_call": {"completed_reads": _read_count(reordered), "repeat_facts": len(_repeat_events(reordered))},
                "history_excluded": {"second_run_reads": _read_count(second_run), "second_run_repeat_facts": len(_repeat_events(second_run))},
                "cancel_hook": {
                    "fact_before_hook": fact.sequence < hook_event.sequence,
                    "hook_repeat_count": hook_payload["repeat_count"],
                    "fact_records": len(fact_events),
                    "hook_records": len(hook_events),
                },
            },
            sort_keys=True,
        )
    )


def _report(call_id: str, tool: str, args: dict[str, object], output: str) -> ReportedCall:
    return ReportedCall(call_id, tool, args, ToolSuccess(tool, output=TextOutput(output)))


def _batch(*reports: ReportedCall) -> TurnBatch:
    calls = tuple(ToolCall(report.final_tool_name, dict(report.final_arguments), tool_call_id=report.tool_call_id) for report in reports)
    return TurnBatch(calls, results=list(reports))


def test_repetition_uses_ordered_authorized_calls_and_exact_typed_results() -> None:
    repetition = ToolBatchRepetition()
    first = _batch(_report("call-1", "read", {"path": "a"}, "same"), _report("call-2", "grep", {"query": "q"}, "two"))
    same_observation_new_ids = _batch(
        _report("call-3", "read", {"path": "a"}, "same"),
        _report("call-4", "grep", {"query": "q"}, "two"),
    )
    changed_result = _batch(_report("call-5", "read", {"path": "a"}, "changed"), _report("call-6", "grep", {"query": "q"}, "two"))
    reversed_order = _batch(_report("call-7", "grep", {"query": "q"}, "two"), _report("call-8", "read", {"path": "a"}, "same"))

    assert repetition.observe(first) is None
    assert repetition.observe(same_observation_new_ids) == 2
    assert repetition.observe(changed_result) is None
    assert repetition.observe(same_observation_new_ids) is None
    assert repetition.observe(reversed_order) is None


def test_partial_batches_do_not_count_and_reset_clears_the_streak() -> None:
    repetition = ToolBatchRepetition()
    complete = _batch(_report("call-1", "read", {"path": "a"}, "same"))
    partial = TurnBatch(complete.calls)

    assert repetition.observe(complete) is None
    assert repetition.observe(partial) is None
    assert repetition.observe(complete) == 2
    repetition.reset()
    assert repetition.observe(complete) is None


def test_json_identity_keeps_boolean_and_number_values_distinct() -> None:
    repetition = ToolBatchRepetition()
    boolean = _batch(_report("call-1", "read", {"flag": True}, "same"))
    number = _batch(_report("call-2", "read", {"flag": 1}, "same"))

    assert repetition.observe(boolean) is None
    assert repetition.observe(number) is None
    assert repetition.observe(boolean) is None


def test_result_kind_and_failure_details_are_part_of_the_observation() -> None:
    repetition = ToolBatchRepetition()
    success = _batch(_report("call-1", "read", {"path": "a"}, "same"))
    failure = TurnBatch(
        calls=success.calls,
        results=[ReportedCall("call-2", "read", {"path": "a"}, ToolFailure("read", "access denied", TextOutput("same")))],
    )

    assert repetition.observe(success) is None
    assert repetition.observe(failure) is None
    assert repetition.observe(success) is None
