"""Workflows plugin: config, snapshots, state, and driver classification without a core."""

import asyncio
import hashlib
from pathlib import Path

import pytest

from voidcode.plugins.workflows.workflows_plugin import (
    WorkflowNode,
    WorkflowRun,
    WorkflowState,
    _CancelledNode,
    _encode_result,
    _ParkedNode,
    _PausedRun,
    _split_group,
    load_workflow_script,
    parse_workflows_config,
    prepare_workflows_deactivation,
    snapshot_workflow_source,
)


def test_config_accepts_workflows_and_reserved_providers_only() -> None:
    assert dict(parse_workflows_config(None)) == {}
    assert dict(parse_workflows_config({"workflows": {"review": "flows/review.py"}, "providers": {}})) == {"review": "flows/review.py"}
    with pytest.raises(ValueError, match="not supported"):
        parse_workflows_config({"workflows": {}, "inline": "run()"})
    with pytest.raises(ValueError, match="not supported"):
        parse_workflows_config({"workflows": {}, "join_slice_seconds": 5})
    for bad in ("../escape.py", "/abs.py", "flows/review.txt"):
        with pytest.raises(ValueError, match="workspace-relative .py"):
            parse_workflows_config({"workflows": {"bad": bad}})
    with pytest.raises(ValueError, match="non-empty string"):
        parse_workflows_config({"workflows": {"bad": ""}})


def test_snapshot_reads_without_executing_and_load_validates_entry(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    script = tmp_path / "evil.py"
    script.write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\nasync def run(ctx, task):\n    return 'ok'\n",
        encoding="utf-8",
    )
    relative, source, digest = snapshot_workflow_source({"evil": "evil.py"}, tmp_path, "evil")
    assert relative == "evil.py" and digest == hashlib.sha256(source).hexdigest()
    assert not marker.exists()
    loaded = load_workflow_script("evil", relative, source)
    assert asyncio.run(loaded.entry(object(), "task")) == "ok"
    with pytest.raises(ValueError, match="unknown workflow"):
        snapshot_workflow_source({"evil": "evil.py"}, tmp_path, "missing")
    with pytest.raises(ValueError, match="must define 'async def run"):
        load_workflow_script("evil", relative, b"async def not_run(ctx, task):\n    return 1\n")
    with pytest.raises(ValueError, match="exactly \\(ctx, task\\)"):
        load_workflow_script("evil", relative, b"async def run(ctx):\n    return 1\n")


def test_run_round_trip_rejects_unknown_and_missing_fields() -> None:
    run = WorkflowRun(
        run_id="r1",
        session_id="s1",
        workflow="review",
        source_path="flows/review.py",
        source_sha256="a" * 64,
        source_artifact="b" * 64,
        task="review this",
        input_sha256="c" * 64,
        nodes=(WorkflowNode(key="a", status="completed", output="done"),),
        result="summary",
        result_bytes=7,
    )
    with pytest.raises(ValueError, match="not supported"):
        WorkflowRun.from_payload({**run.to_payload(), "inline": True})
    tampered = run.to_payload()
    del tampered["task"]
    with pytest.raises(ValueError, match="missing field"):
        WorkflowRun.from_payload(tampered)


def test_deactivation_parks_live_runs_and_keeps_terminal_ones() -> None:
    def _run(run_id: str, status: str) -> WorkflowRun:
        return WorkflowRun(
            run_id=run_id,
            session_id="s",
            workflow="w",
            source_path="w.py",
            source_sha256="a" * 64,
            source_artifact="b" * 64,
            task="t",
            input_sha256="c" * 64,
            status=status,  # type: ignore[arg-type]
        )

    payload = {"schema_version": 1, "runs": {rid: _run(rid, status).to_payload() for rid, status in (("a", "running"), ("b", "completed"))}}
    parked = WorkflowState.from_payload(prepare_workflows_deactivation(payload, "session", "s", 1))
    assert parked.runs["a"].status == "parked"
    assert parked.runs["b"].status == "completed"


def test_group_split_keeps_rests_apart_from_failures() -> None:
    group = BaseExceptionGroup("nodes", [_ParkedNode("a", "waiting", None), _PausedRun(), ValueError("boom")])
    rests, failures = _split_group(group)
    assert len(rests) == 2 and len(failures) == 1
    nested = BaseExceptionGroup("outer", [group, _CancelledNode("b")])
    rests, failures = _split_group(nested)
    assert len(rests) == 3 and len(failures) == 1


def test_encode_result_distinguishes_null_empty_and_rejects_other_objects() -> None:
    assert _encode_result(None) == "null"
    assert _encode_result("") == ""
    assert _encode_result("null") == "null"
    with pytest.raises(ValueError):
        _encode_result(object())
