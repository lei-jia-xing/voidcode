"""Wakeup facts are delivered from the atomic claim, live and after recovery."""

import json
import time

from tests.unit.core.test_nested_tools import _server, _turn
from voidcode.core.activation import BUNDLED_PLUGIN_IDS
from voidcode.core.bootstrap import open_core
from voidcode.sdk.hooks import WakeupFiredFact

PLUGIN = """
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.hooks import WakeupFiredFact
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.json_values import json_wire_object

class Arm:
    definition = CommandDefinition("arm", "arm a wakeup", {"type": "object"}, effects=frozenset({ToolEffect.SESSION}))
    def invoke(self, invocation, services):
        identity = services.schedule_wakeup({"text": "wake up", "kind": "probe"}, due_at_ms=invocation.arguments["due"], role="user")
        return CommandResult(status="ok", data={"wakeup_id": identity})

def observe(fact, context, services):
    if isinstance(fact, WakeupFiredFact):
        services.stage_event("seen", {"sequence": fact.event_sequence, "owner": fact.owner, "id": fact.wakeup_id,
            "run": fact.run_id, "payload": json_wire_object(fact.payload), "role": fact.role})

def create_plugin(config):
    return Plugin(commands=(Arm(),), observer=observe, observed_facts=("wakeup_fired",))
"""


def _fact(core, session):
    cursor = 0
    while True:
        page = core.plugin_runtime.fact_page(session, cursor, 200)
        found = next((fact for fact in page.facts if isinstance(fact, WakeupFiredFact)), None)
        if found is not None:
            return found
        if not page.has_more:
            raise AssertionError("no wakeup fact")
        cursor = page.next_sequence


def _seen(core, session):
    return [event for event in core.store.events(session) if event.event_type == "plugin.probe.seen"]


def test_claim_fact_is_live_before_dispatch_and_recovery_delivers_it_once(tmp_path):
    source = tmp_path / "probe"
    source.mkdir()
    (source / "__init__.py").write_text(PLUGIN)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    due = int(time.time() * 1000) + 600_000
    with _server([_turn(output="awake")]) as server:
        plugins = {name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS}
        plugins["probe"] = {"path": str(source)}
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/test-model"},
                    "policy": {"approval_mode": "yolo"},
                    "providers": {"endpoint": {"api_key": "test", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                    "plugins": plugins,
                }
            )
        )
        with open_core(workspace, state_dir=state) as core:
            tick = core.tick
            core.tick = lambda now_ms=None: {"fired": None}
            live = core.create_session("live").session_id
            armed = core.invoke_command(live, "arm", {"due": due}, expected_revision=core.revision_id)
            assert armed.status == "ok"
            assert tick(now_ms=due)["fired"] == armed.data["wakeup_id"]
            fact = _fact(core, live)
            assert fact.owner == "probe" and fact.wakeup_id == armed.data["wakeup_id"]
            assert fact.payload == {"text": "wake up", "kind": "probe"} and fact.role == "user"
            seen = _seen(core, live)
            assert len(seen) == 1 and seen[0].payload["sequence"] == fact.event_sequence
            dispatch = next(event for event in core.store.events(live) if event.event_type == "kernel.provider_intent")
            assert fact.event_sequence < seen[0].sequence < dispatch.sequence
            fired = core.store.events(live)[fact.event_sequence - 1]
            assert tuple(fired.payload["eligible_owners"]) == ("probe",)
            recovered = core.create_session("recovered").session_id
            arm = core.invoke_command(recovered, "arm", {"due": due + 1}, expected_revision=core.revision_id)
            claim = core.store.claim_and_start_wakeup(
                str(workspace), due + 1, run_id="recover-run", logical_turn="recover-turn", revision_id=core.revision_id, eligible_owners=("probe",)
            )
            assert claim[0].wakeup_id == arm.data["wakeup_id"]
            assert _seen(core, recovered) == []
        for _ in range(2):
            with open_core(workspace, state_dir=state) as core:
                core.tick = lambda now_ms=None: {"fired": None}
                assert _fact(core, live) == fact
                assert len(_seen(core, live)) == 1
                restored = _fact(core, recovered)
                observations = _seen(core, recovered)
                assert len(observations) == 1
                assert observations[0].payload["run"] == restored.run_id == "recover-run"
                assert observations[0].payload["sequence"] == restored.event_sequence
