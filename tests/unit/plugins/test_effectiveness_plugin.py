"""Public-fact projections stay independent of tool names and private event payloads."""

from dataclasses import replace

from voidcode.plugins.effectiveness import create_plugin
from voidcode.plugins.effectiveness.effectiveness_plugin import (
    project_session_effectiveness,
    read_session_facts,
    render_summary,
)
from voidcode.sdk.hooks import (
    ApprovalRequestedFact,
    ChildSpawnedFact,
    ContextProjectedFact,
    FactPage,
    InvocationFinishedFact,
    RunRequestedFact,
)
from voidcode.sdk.services import SessionUsageReport
from voidcode.sdk.tools import ReadObservation, ResourceChange

COMMON = dict(session_id="session", run_id="run", revision_id="revision", logical_turn="turn")


def outcome(sequence=1, tool="custom_reader", **kwargs):
    return InvocationFinishedFact(
        **COMMON,
        event_sequence=sequence,
        tool_name=tool,
        tool_call_id=str(sequence),
        status="ok",
        semantics=(),
        argument_bytes=10,
        result_bytes=20,
        **kwargs,
    )


def usage(**kwargs):
    return SessionUsageReport(
        session_id="session",
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=40,
        uncached_input_tokens=60,
        total_cost_usd=0.1,
        turn_count=1,
        **kwargs,
    )


def project(facts, report=None):
    return project_session_effectiveness(workspace_id="workspace", session_id="session", facts=facts, usage=report or usage())


def read(lines=(1,), *, digest="a", whole=False, clipped=False):
    return ReadObservation("file.py", digest * 64, lines, 3, whole, clipped)


def test_custom_names_and_batch_resources_do_not_change_meaning():
    facts = [
        outcome(reads=(read(), read((2,)))),
        outcome(2, reads=(read(),)),
        outcome(3, reads=(read((3,)),)),
        replace(
            outcome(4, "replace_everything"),
            effects=("write",),
            model="model",
            changes=(ResourceChange("a.py"), ResourceChange("b.py")),
        ),
        ChildSpawnedFact(**COMMON, event_sequence=5, child_session_id="child-a"),
        ChildSpawnedFact(**COMMON, event_sequence=6, child_session_id="child-b"),
    ]
    report = project(facts)
    assert report.repeated_read_count == 1
    assert report.followup_read_count == 2
    assert report.delegated_task_count == 2
    assert report.models[0].edit_calls == 1
    assert report.tool_call_count == 4
    renamed = [replace(f, tool_name="another_name") if isinstance(f, InvocationFinishedFact) else f for f in facts]
    assert project(renamed).repeated_read_count == report.repeated_read_count
    assert project(renamed).models == report.models


def test_different_versions_and_duplicate_partial_ranges_are_not_followups():
    report = project(
        [
            outcome(reads=(read(),)),
            outcome(2, reads=(read(),)),
            outcome(3, reads=(read((2,), digest="b"),)),
            outcome(4, reads=(read((1, 2, 3), whole=True),)),
            outcome(5, reads=(read((1, 2, 3), whole=True),)),
        ]
    )
    assert report.repeated_read_count == 2
    assert report.followup_read_count == 1


def test_errors_retries_diagnostics_and_model_edit_attribution():
    failed = replace(
        outcome(tool="custom_edit"),
        status="error",
        error="ambiguous",
        effects=("write",),
        model="model",
        error_kind="ambiguous_match",
        has_retry_guidance=True,
    )
    success = replace(failed, event_sequence=2, status="ok", error=None, error_kind=None, has_retry_guidance=False)
    report = project([failed, success])
    row = report.tools[0]
    assert (row.calls, row.errors, row.successes, row.retries_after_error, row.retry_guidance_count) == (2, 1, 1, 1, 1)
    assert row.argument_bytes == 20
    assert row.result_bytes == 40
    assert report.models[0].edit_ambiguous_match_rate == 0.5
    assert "custom_edit" in render_summary(report)


def test_actual_resume_and_projection_facts_have_no_plugin_dependency():
    report = project(
        [
            RunRequestedFact(**COMMON, event_sequence=1, resumed=False),
            RunRequestedFact(**COMMON, event_sequence=2, resumed=False),
            RunRequestedFact(**COMMON, event_sequence=3, resumed=True),
            ApprovalRequestedFact(**COMMON, event_sequence=4, request_id="approval"),
            ContextProjectedFact(**COMMON, event_sequence=5, owner="custom_compressor", covered_bytes=100, dropped_bytes=50),
            outcome(6, "task"),
        ]
    )
    assert (report.resumed_run_count, report.approval_request_count, report.compaction_count) == (1, 1, 1)
    assert report.delegated_task_count == 0


def test_empty_source_pages_still_advance():
    class Services:
        calls = []

        def read_facts(self, *, after_sequence, limit):
            self.calls.append(after_sequence)
            if after_sequence == 0:
                return FactPage((), 200, True)
            if after_sequence == 200:
                return FactPage((outcome(201),), 400, True)
            return FactPage((), 405, False)

    services = Services()
    assert len(tuple(read_session_facts(services))) == 1
    assert services.calls == [0, 200, 400]


def test_empty_and_unknown_usage_are_truthful_and_plugin_is_read_only():
    report = project([])
    assert report.success_rate is None
    assert report.cache_hit_rate == 0.4
    assert "No persisted tool calls" in render_summary(report)
    unknown = replace(usage(), input_tokens=None, has_unknown_usage=True)
    assert project([], unknown).input_tokens is None
    assert project([], unknown).has_unknown_usage
    plugin = create_plugin()
    assert len(plugin.commands) == 1
    assert not plugin.tools


def test_effectiveness_command_pages_readable_metrics_and_preserves_unknown(tmp_path):
    from voidcode.plugins.effectiveness.effectiveness_plugin import EffectivenessCommand
    from voidcode.sdk.commands import CommandInvocation
    from voidcode.sdk.invocation import ToolContext
    from voidcode.sdk.views import MarkdownViewData

    facts = tuple(outcome(index + 1, tool=f"tool-{index}") for index in range(12))

    class Services:
        def read_facts(self, *, after_sequence, limit):
            return FactPage(facts, 12, False)

        def get_usage(self):
            return replace(usage(), input_tokens=None, total_cost_usd=None, has_unknown_usage=True)

    context = ToolContext(tmp_path, "session", "run", "inv", "rev", "user")
    command = EffectivenessCommand()
    first = command.invoke(CommandInvocation(context, "effectiveness"), Services())
    view = MarkdownViewData.from_payload(first.presentation.data)
    assert "Input tokens: Unknown" in view.content
    assert "Cost (USD): Unknown" in view.content
    assert "incomplete or unknown" in view.content
    assert view.content.count("## tool-") == 10
    action = next(action for action in view.actions if action.label == "Next metrics")
    second = command.invoke(CommandInvocation(context, action.action, action.arguments), Services())
    assert MarkdownViewData.from_payload(second.presentation.data).content.count("## tool-") == 2
    assert second.data["provider_usage"]["input_tokens"] is None
    assert len(first.data["tools"]) == 12


def test_effectiveness_factory_rejects_unsupported_config():
    import pytest

    assert create_plugin({"providers": {}}).commands
    with pytest.raises(ValueError, match="effectiveness config does not support field: enabled"):
        create_plugin({"enabled": True})
