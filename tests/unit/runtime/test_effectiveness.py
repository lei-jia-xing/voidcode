from __future__ import annotations

from voidcode.runtime.effectiveness import ToolEffectivenessEvent, project_tool_effectiveness
from voidcode.runtime.events import EventEnvelope


def _completed(
    *,
    session_id: str,
    sequence: int,
    tool: str,
    status: str,
    **payload: object,
) -> ToolEffectivenessEvent:
    return ToolEffectivenessEvent(
        session_id=session_id,
        event=EventEnvelope(
            session_id=session_id,
            sequence=sequence,
            event_type="runtime.tool_completed",
            source="tool",
            payload={"tool": tool, "status": status, **payload},
        ),
    )


def test_project_tool_effectiveness_aggregates_errors_retries_and_pressure() -> None:
    report = project_tool_effectiveness(
        workspace_id="/workspace",
        session_ids=("s1", "s2"),
        session_metadata={
            "s1": {
                "provider_usage": {
                    "cumulative": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_tokens": 30,
                        "cache_write_tokens": 4,
                        "uncached_input_tokens": 70,
                    }
                }
            }
        },
        events=(
            _completed(
                session_id="s1",
                sequence=1,
                tool="edit",
                status="error",
                arguments={"path": "secret.py", "oldString": "secret"},
                content="failed",
                error="stale",
                error_kind="stale_edit",
                retry_guidance="read again",
            ),
            _completed(
                session_id="s1",
                sequence=2,
                tool="edit",
                status="ok",
                arguments={"path": "secret.py"},
                content="updated",
            ),
            _completed(
                session_id="s2",
                sequence=1,
                tool="read_file",
                status="ok",
                arguments={"path": "large.txt"},
                content="bounded result",
                truncated=True,
                partial=True,
            ),
            _completed(
                session_id="s2",
                sequence=2,
                tool="read_file",
                status="ok",
                arguments={"path": "large.txt", "offset": 2001},
                content="continued",
            ),
            ToolEffectivenessEvent(
                session_id="s1",
                event=EventEnvelope(
                    session_id="s1",
                    sequence=3,
                    event_type="runtime.context_compacted",
                    source="runtime",
                    payload={},
                ),
            ),
            ToolEffectivenessEvent(
                session_id="s1",
                event=EventEnvelope(
                    session_id="s1",
                    sequence=4,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={},
                ),
            ),
            ToolEffectivenessEvent(
                session_id="s1",
                event=EventEnvelope(
                    session_id="s1",
                    sequence=5,
                    event_type="runtime.request_received",
                    source="runtime",
                    payload={},
                ),
            ),
        ),
    )

    assert report.session_count == 2
    assert report.tool_call_count == 4
    assert report.success_count == 3
    assert report.error_count == 1
    edit = next(tool for tool in report.tools if tool.tool == "edit")
    assert edit.calls == 2
    assert edit.retries_after_error == 1
    assert edit.retry_guidance_count == 1
    assert edit.error_kinds == {"stale_edit": 1}
    read = next(tool for tool in report.tools if tool.tool == "read_file")
    assert read.truncated_results == 1
    assert read.partial_results == 1
    assert report.repeated_read_count == 1
    assert report.followup_read_count == 1
    assert report.compaction_count == 1
    assert report.resumed_run_count == 1
    assert report.input_tokens == 100
    assert report.output_tokens == 20
    assert report.cache_read_tokens == 30
    assert report.cache_write_tokens == 4
    assert report.uncached_input_tokens == 70
    assert report.cache_hit_rate == 0.3

    payload = report.to_payload()
    assert payload["privacy"] == {
        "source": "persisted_runtime_events",
        "stores_source_content": False,
        "stores_arguments": False,
        "projection": "aggregate_only",
    }
    assert "secret.py" not in str(payload)
    assert "bounded result" not in str(payload)


def test_project_tool_effectiveness_handles_empty_history() -> None:
    report = project_tool_effectiveness(
        workspace_id="/workspace",
        session_ids=(),
        events=(),
    )

    assert report.tool_call_count == 0
    assert report.success_rate is None
    assert report.tools == ()
