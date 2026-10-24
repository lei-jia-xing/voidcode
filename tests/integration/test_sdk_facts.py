"""A third-party plugin sees the same facts live, through paging, and after restart."""

import json
from dataclasses import asdict

from tests.unit.core.test_nested_tools import _call, _server, _turn
from voidcode.core.bootstrap import open_core
from voidcode.core.storage.store import EventDraft
from voidcode.sdk.hooks import ContextProjectedFact, InvocationFinishedFact, RequestWindow
from voidcode.sdk.json_values import json_wire_value

PLUGIN = """
from dataclasses import asdict
from typing import get_args
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.hooks import ObservedFactKind, RunFinishedFact, UsageFact
from voidcode.sdk.tools import (ToolDefinition, ToolEffect, ToolSuccess, TextOutput, OutputBounds, ReadObservation, ResourceChange)

class Substitute:
    definition = ToolDefinition("replace_any_name", "replacement", {"type": "object"},
        effects=frozenset({ToolEffect.WRITE}),
        path_argument_keys=("targets[].path",))
    def invoke(self, call, *, context, services):
        return ToolSuccess(call.tool_name, TextOutput("done", bounds=OutputBounds(partial=True)),
            coverage=(ReadObservation("a.py", "a" * 64, (1,), 2),),
            changes=(ResourceChange("a.py"), ResourceChange("b.py")))

_spawned = False

def observe(fact, context, services):
    global _spawned
    if context.session_id == "parent" and isinstance(fact, UsageFact) and not _spawned:
        _spawned = True
        services.spawn_child("child prompt")
    if isinstance(fact, RunFinishedFact) and services.window_projection() is None:
        services.project_window(covered_from_sequence=1, covered_to_sequence=fact.event_sequence, replacement="summary")
    services.stage_event("seen", {"fact": asdict(fact)})

def post_tool(outcome, context, services):
    return "extra note"

def create_plugin(config):
    return Plugin(tools=(Substitute(),), observer=observe, observed_facts=get_args(ObservedFactKind), post_tool=post_tool)
"""


def history(core, session_id):
    cursor = 0
    facts = []
    while True:
        page = core.plugin_runtime.fact_page(session_id, cursor, 1)
        facts.extend(page.facts)
        cursor = page.next_sequence
        if not page.has_more:
            return facts


def test_live_history_and_restart_preserve_original_declaration_and_observations(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "__init__.py").write_text(PLUGIN)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    with _server([_turn(_call("call", "replace_any_name", targets=[{"path": "c.py"}])), _turn(output="done"), _turn(output="done")]) as server:
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/exact-model"},
                    "policy": {"approval_mode": "yolo"},
                    "providers": {"endpoint": {"api_key": "test", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                    "plugins": {"probe": {"path": str(source)}},
                }
            )
        )
        with open_core(workspace, state_dir=state_dir) as core:
            result = core.run("exercise substitute", session_id="parent")
            assert result.status == "completed", result.error
            children = [
                event.payload["child_session_id"] for event in core.store.events(result.session_id) if event.event_type == "core.child_spawned"
            ]
            assert len(children) == 1
            core.join_child(result.session_id, children[0], timeout_seconds=15)
            facts = history(core, result.session_id)
            observed = [e.payload["fact"] for e in core.store.events(result.session_id) if e.event_type == "plugin.probe.seen"]
            assert json_wire_value(observed) == json_wire_value([asdict(fact) for fact in facts])
            invocation = next(fact for fact in facts if isinstance(fact, InvocationFinishedFact))
            assert invocation.partial is True
            assert len(invocation.reads) == 1 and len(invocation.changes) == 2
            assert any(isinstance(fact, ContextProjectedFact) for fact in facts)
            assert invocation.owner == "probe" and invocation.model == "exact-model"
            assert str(workspace / "c.py") in invocation.resource_paths
            assert invocation.argument_bytes > 0 and invocation.result_bytes > 0
            expected = json_wire_value([asdict(fact) for fact in facts])
        # Current catalog changes are not a source of historical semantic truth.
        (source / "__init__.py").write_text(PLUGIN.replace("effects=frozenset({ToolEffect.WRITE})", "effects=frozenset({ToolEffect.READ})"))
        with open_core(workspace, state_dir=state_dir) as core:
            assert json_wire_value([asdict(fact) for fact in history(core, result.session_id)]) == expected
            assert len([e for e in core.store.events(result.session_id) if e.event_type == "plugin.probe.seen"]) == len(facts)

            orphan = core.create_session("orphan")
            core.store.commit_staged(
                orphan.session_id,
                run_id="orphan-run",
                revision_id=core.revision_id,
                status="interrupted",
                checkpoint_kind="terminal",
                checkpoint_payload={"stop_cause": "shutdown"},
                events=(
                    EventDraft(
                        "core.provider_intent",
                        "kernel",
                        "model",
                        {
                            "provider": "endpoint",
                            "model": "exact-model",
                            "logical_turn": "orphan-turn",
                            "request_window": RequestWindow(message_characters=0, tool_schema_characters=0).as_payload(),
                            "eligible_owners": ["probe"],
                        },
                    ),
                    EventDraft(
                        "core.run_interrupted",
                        "kernel",
                        "kernel",
                        {
                            "stop_cause": "shutdown",
                            "logical_turn": "orphan-turn",
                            "eligible_owners": ["probe"],
                        },
                    ),
                ),
            )
        with open_core(workspace, state_dir=state_dir) as core:
            restored = history(core, "orphan")
            observed = [e.payload["fact"] for e in core.store.events("orphan") if e.event_type == "plugin.probe.seen"]
            assert json_wire_value(observed) == json_wire_value([asdict(fact) for fact in restored])
            assert [fact.event_sequence for fact in restored] == sorted(fact.event_sequence for fact in restored)
