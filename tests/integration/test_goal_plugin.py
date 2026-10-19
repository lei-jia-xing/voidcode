"""End-to-end integration tests for the Goal plugin.

Drives real kernel, plugin host, loopback provider, and durable storage boundaries:
- Goal creation, update, and get via tool invocations
- Autonomous execution and verification of multi-step task
- Process restart preserving goal state and active windows
- Pause, resume, and edit workflows with budget window baselines
- Budget enforcement and unknown-usage safety gate
- Unfinished goal replacement confirmation
- Long user objective (>4k + Unicode) spill, read-back, and tamper detection
- Second-plugin parity ensuring namespace isolation
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest

from tests.provider_loopback import OpenAILoopbackServer, _OpenAILoopbackHandler, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel
from voidcode.kernel.storage.store import EventDraft, SessionRecord
from voidcode.sdk.codec import encode_provider_result
from voidcode.sdk.commands import CommandResult
from voidcode.sdk.manifest import (
    PluginCommandDeclaration,
    PluginFileIdentity,
    PluginManifest,
)
from voidcode.sdk.provider import ProviderTokenUsage, ProviderTurnResult
from voidcode.sdk.tools import ToolEffect

_GOAL_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "voidcode" / "plugins" / "goal"


def _second_plugin(root: Path) -> dict[str, object]:
    """Second plugin fixture to verify namespace and budget isolation."""
    root.mkdir(parents=True, exist_ok=True)
    plugin_file = root / "second_plugin.py"
    code = """from voidcode.sdk.plugin import Plugin
from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.tools import ToolEffect

class SecondCommand:
    definition = CommandDefinition(
        name="second_cmd",
        description="Second plugin command",
        argument_schema={"type": "object"},
        effects=frozenset({ToolEffect.SESSION}),
    )
    def invoke(self, invocation, services):
        services.replace_state({"second_key": "second_val"}, expected_version=0, scope="session")
        return CommandResult(status="ok", output="Second command ok", data={"second": True})

def create_plugin(config):
    return Plugin(commands=(SecondCommand(),))
"""
    plugin_file.write_text(code, encoding="utf-8")
    digest = hashlib.sha256(plugin_file.read_bytes()).hexdigest()
    manifest = PluginManifest(
        plugin_id="second-plugin",
        version="1.0.0",
        entrypoint="second_plugin:create_plugin",
        files=(PluginFileIdentity(path="second_plugin.py", sha256=digest),),
        commands=(
            PluginCommandDeclaration(
                name="second_cmd",
                description="Second plugin command",
                argument_schema={"type": "object"},
                effects=(ToolEffect.SESSION,),
            ),
        ),
    )
    (root / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")
    return {"second-plugin": {"path": str(root)}}


@contextmanager
def _loopback_server(
    replies: list[dict[str, object]],
    factory: Callable[[list[dict[str, object]]], OpenAILoopbackServer] = OpenAILoopbackServer,
) -> Iterator[OpenAILoopbackServer]:
    server = factory(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


class _InFlightHandler(_OpenAILoopbackHandler):
    """Loopback handler that observes kernel state while the request is still in flight."""

    def do_POST(self) -> None:
        cast(_InFlightLoopbackServer, self.server).observe_before_reply()
        super().do_POST()


class _InFlightLoopbackServer(OpenAILoopbackServer):
    """Real listening provider that hands the test one observation before answering each request."""

    def __init__(self, replies: list[dict[str, object]], observe: Callable[[int], None]) -> None:
        super().__init__(replies)
        self.RequestHandlerClass = _InFlightHandler
        self._observe = observe
        self.request_count = 0

    def observe_before_reply(self) -> None:
        """Observe before a single byte of the reply is written, so the bill cannot hide a delay."""
        self.request_count += 1
        self._observe(self.request_count)


_CREATED_KERNELS: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_created_kernels() -> Iterator[None]:
    """Close every kernel this test opened, also when the body raises."""
    yield
    while _CREATED_KERNELS:
        _CREATED_KERNELS.pop().close()


def _kernel_with_loopback(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    extra_plugins: dict[str, object] | None = None,
) -> Kernel:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    plugins_cfg: dict[str, object] = {
        "goal": {
            "path": str(_GOAL_PLUGIN_ROOT),
        }
    }
    if extra_plugins:
        plugins_cfg.update(extra_plugins)

    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": plugins_cfg,
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    trusted = frozenset(plugins_cfg.keys())
    kernel = open_kernel(workspace, state_dir=state_dir, trusted_plugins=trusted)
    _CREATED_KERNELS.append(kernel)
    return kernel


def _set_goal_enabled(config_path: Path, enabled: bool) -> None:
    """Rewrite the workspace configuration the way a user disables or re-enables the owner."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["plugins"]["goal"]["enabled"] = enabled
    config_path.write_text(json.dumps(config), encoding="utf-8")


def _invoke(kernel: Kernel, session_id: str, name: str, arguments: Mapping[str, object]) -> CommandResult:
    """Invoke a command bound to the revision the client just displayed."""
    return kernel.invoke_command(session_id, name, arguments, expected_revision=kernel.command_catalog().revision_id)


def _confirm(kernel: Kernel, session_id: str, request_id: str, decision: Literal["allow", "deny"]) -> CommandResult:
    """Resolve a confirmation bound to the revision the client just displayed."""
    return kernel.confirm_action(session_id, request_id, decision, expected_revision=kernel.command_catalog().revision_id)


def test_goal_plugin_commands_and_lifecycle(tmp_path: Path) -> None:
    """Command routing, state inspection and lifecycle transitions on the real stack.

    No provider replies are supplied: the automatic turn `goal.set` drives fails fast, so
    the goal stays active for the pause/resume assertions and the command surface is shown
    not to block on a driven turn.
    """
    # Empty reply list: the driven turn fails fast at the provider (still active afterwards).
    server_replies: list[dict[str, object]] = []
    with _loopback_server(server_replies) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        session_id = kernel.create_session().session_id

        # 1. goal query on empty session -> None
        res_empty = _invoke(kernel, session_id, "goal", {})
        assert res_empty.status == "ok"
        assert res_empty.data["goal"] is None

        # 2. goal.set: create active unbudgeted goal
        res_set = _invoke(
            kernel,
            session_id,
            "goal.set",
            {"objective": "Create microservice auth endpoint"},
        )
        assert res_set.status == "ok"
        goal_data = res_set.data["goal"]
        assert goal_data["status"] == "active"
        assert goal_data["token_budget"] is None
        assert goal_data["tokens_used"] == 0

        # 3. goal query -> returns active goal details
        res_get = _invoke(kernel, session_id, "goal", {})
        assert res_get.status == "ok"
        assert res_get.data["goal"]["objective"] == "Create microservice auth endpoint"
        assert res_get.data["remaining_tokens"] is None

        # 4. goal.edit -> updates objective while preserving tokens_used and timestamps
        res_edit = _invoke(
            kernel,
            session_id,
            "goal.edit",
            {"objective": "Create microservice auth endpoint with OAuth2"},
        )
        assert res_edit.status == "ok"
        assert res_edit.data["goal"]["objective"] == "Create microservice auth endpoint with OAuth2"

        # 5. goal.pause -> closes active window, clears budget constraint
        res_pause = _invoke(kernel, session_id, "goal.pause", {})
        assert res_pause.status == "ok"
        assert res_pause.data["goal"]["status"] == "paused"

        # 6. goal.resume -> opens fresh window, re-establishes remaining budget
        res_resume = _invoke(kernel, session_id, "goal.resume", {})
        assert res_resume.status == "ok"
        assert res_resume.data["goal"]["status"] == "active"

        # 7. goal.set to replace unfinished goal -> requires confirmation when not actor='user'
        res_replace = _invoke(kernel, session_id, "goal.set", {"objective": "Brand new goal"})
        assert res_replace.status == "pending_confirmation"
        assert res_replace.confirmation is not None
        assert res_replace.confirmation.owner == "goal"

        # Confirmed replacement succeeds via core confirmation resolution (actor='user')
        req_id = res_replace.confirmation.request_id
        res_replace_ok = _confirm(kernel, session_id, req_id, "allow")
        assert res_replace_ok.status == "ok"
        assert res_replace_ok.data["goal"]["objective"] == "Brand new goal"

        # 8. goal.clear: explicit clear without confirmation prompt
        res_clear = _invoke(kernel, session_id, "goal.clear", {})
        assert res_clear.status == "ok"
        assert res_clear.data["cleared"] is True

        res_final = _invoke(kernel, session_id, "goal", {})
        assert res_final.data["goal"] is None


def test_goal_autonomous_execution_and_completion(tmp_path: Path) -> None:
    """Test full loopback execution where model creates a goal, reads two integers from input.txt,
    computes the sum, writes sum.txt, reads back to verify, and marks the goal complete."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.txt").write_text("17 25", encoding="utf-8")

    # Turn 1: user says "start". Model creates goal via create_goal tool.
    call_create = {
        "id": "call-create-1",
        "type": "function",
        "function": {
            "name": "create_goal",
            "arguments": json.dumps({"objective": "Read input.txt, write sum to sum.txt, verify, and complete", "token_budget": 5000}),
        },
    }
    turn_1 = openai_response(
        message={"role": "assistant", "content": None, "tool_calls": [call_create]},
        finish="tool_calls",
        usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
    )

    # Turn 2: model reads input.txt
    call_read_input = {
        "id": "call-read-1",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": json.dumps({"path": "input.txt"}),
        },
    }
    turn_2 = openai_response(
        message={"role": "assistant", "content": "Reading input.txt to extract numbers.", "tool_calls": [call_read_input]},
        finish="tool_calls",
        usage={"prompt_tokens": 70, "completion_tokens": 25, "total_tokens": 95},
    )

    # Turn 3: model writes sum.txt with "42"
    call_write_sum = {
        "id": "call-write-1",
        "type": "function",
        "function": {
            "name": "write",
            "arguments": json.dumps({"path": "sum.txt", "content": "42"}),
        },
    }
    turn_3 = openai_response(
        message={"role": "assistant", "content": "Writing computed sum 42 to sum.txt.", "tool_calls": [call_write_sum]},
        finish="tool_calls",
        usage={"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110},
    )

    # Turn 4: model reads back sum.txt to verify
    call_verify = {
        "id": "call-read-verify",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": json.dumps({"path": "sum.txt"}),
        },
    }
    turn_4 = openai_response(
        message={"role": "assistant", "content": "Verifying sum.txt content.", "tool_calls": [call_verify]},
        finish="tool_calls",
        usage={"prompt_tokens": 85, "completion_tokens": 20, "total_tokens": 105},
    )

    # Turn 5: model calls update_goal(status="complete")
    call_complete = {
        "id": "call-complete-1",
        "type": "function",
        "function": {
            "name": "update_goal",
            "arguments": json.dumps({"status": "complete"}),
        },
    }
    turn_5 = openai_response(
        message={"role": "assistant", "content": "Verified sum.txt contains 42. Marking goal complete.", "tool_calls": [call_complete]},
        finish="tool_calls",
        usage={"prompt_tokens": 90, "completion_tokens": 15, "total_tokens": 105},
    )

    # Turn 6: final model response concluding turn
    turn_6 = openai_response(
        message={"role": "assistant", "content": "Task completed successfully: sum 42 verified in sum.txt."},
        finish="stop",
        usage={"prompt_tokens": 40, "completion_tokens": 15, "total_tokens": 55},
    )

    replies = [turn_1, turn_2, turn_3, turn_4, turn_5, turn_6]
    with _loopback_server(replies) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        session_id = kernel.create_session().session_id

        # Run user turn
        run_res = kernel.run("Start the automated pipeline", session_id=session_id)
        assert run_res.status == "completed"

        # Verify sum.txt exists and contains "42"
        sum_file = workspace / "sum.txt"
        assert sum_file.is_file()
        assert sum_file.read_text(encoding="utf-8") == "42"

        # Check goal state in session
        res_goal = _invoke(kernel, session_id, "goal", {})
        assert res_goal.data["goal"] is not None
        assert res_goal.data["goal"]["status"] == "complete"
        assert res_goal.data["goal"]["tokens_used"] > 0


def test_goal_set_on_idle_session_starts_automatic_work(tmp_path: Path) -> None:
    """`goal.set` on an idle session must drive the whole objective by itself.

    No user prompt is sent and no manual drain is called: the kernel consumes the durable
    continuation, the goal context reaches the model, and the model works the objective
    (read numbers -> write sum -> verify -> complete) against durable state.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.txt").write_text("17 25", encoding="utf-8")

    def call(idx: int, name: str, args: dict[str, object]) -> dict[str, object]:
        return {"id": f"auto-{idx}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    replies = [
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [call(1, "read", {"path": "input.txt"})]},
            finish="tool_calls",
            usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        ),
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [call(2, "write", {"path": "sum.txt", "content": "42"})]},
            finish="tool_calls",
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        ),
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [call(3, "read", {"path": "sum.txt"})]},
            finish="tool_calls",
            usage={"prompt_tokens": 55, "completion_tokens": 20, "total_tokens": 75},
        ),
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [call(4, "update_goal", {"status": "complete"})]},
            finish="tool_calls",
            usage={"prompt_tokens": 60, "completion_tokens": 15, "total_tokens": 75},
        ),
        openai_response(
            message={"role": "assistant", "content": "Sum verified in sum.txt; goal complete."},
            finish="stop",
            usage={"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        session_id = kernel.create_session().session_id

        res_set = _invoke(
            kernel,
            session_id,
            "goal.set",
            {"objective": "Read two integers from input.txt, write their sum to sum.txt, verify it, then complete"},
        )
        assert res_set.status == "ok"

        # Real dispatches happened without a user prompt and without a manual drain call.
        assert len(server.requests) >= 4
        assert (workspace / "sum.txt").read_text(encoding="utf-8") == "42"

        # The goal instructions arrive in the legacy user role, verbatim: with no user prompt in
        # this session the user messages of the first dispatch are the goal contribution alone, and
        # no kernel `[<owner> context]` wrapper is applied to them.
        first_messages = server.requests[0]["messages"]
        assert isinstance(first_messages, list)
        user_contents = [str(message["content"]) for message in first_messages if message["role"] == "user"]
        assert any("Read two integers from input.txt" in content for content in user_contents)
        assert not any(str(message["content"]).startswith("[goal context]") for message in first_messages)

        goal_res = _invoke(kernel, session_id, "goal", {})
        goal = goal_res.data["goal"]
        assert goal["status"] == "complete"
        assert goal["tokens_used"] is not None
        assert goal["tokens_used"] > 0
        assert goal["time_used_seconds"] is not None


def test_process_restart_preserves_goal(tmp_path: Path) -> None:
    """Restart preserves the truthful state of a budgeted goal.

    The single driven dispatch gets no provider reply, so its usage is unreported: the
    contract is `usage-limited` with unknown token counters (never 0) and no additional
    provider request. A fresh kernel over the same workspace must show exactly that.
    """
    # No replies: the one driven dispatch fails, producing an unreported usage fact.
    replies: list[dict[str, object]] = []
    with _loopback_server(replies) as server:
        kernel_1 = _kernel_with_loopback(tmp_path, server)
        session_id = kernel_1.create_session().session_id
        _invoke(
            kernel_1,
            session_id,
            "goal.set",
            {"objective": "Durable task across restarts", "token_budget": 4000},
        )
        res_before = _invoke(kernel_1, session_id, "goal", {})
        before = res_before.data["goal"]
        goal_id = before["goal_id"]

        assert before["status"] == "usage-limited"
        assert before["tokens_used"] is None
        assert before["window_tokens_used"] is None
        assert before["has_unknown_usage"] is True
        assert before["token_budget"] == 4000
        billing_requests = len(server.requests)
        assert billing_requests == 1  # the unknown dispatch is not retried

        kernel_1.close()

        kernel_2 = _kernel_with_loopback(tmp_path, server)
        res_after = _invoke(kernel_2, session_id, "goal", {})
        after = res_after.data["goal"]

        assert after["goal_id"] == goal_id
        assert after["status"] == "usage-limited"
        assert after["tokens_used"] is None
        assert after["window_tokens_used"] is None
        assert after["has_unknown_usage"] is True
        assert after["token_budget"] == 4000
        assert after["objective"] == "Durable task across restarts"
        # Restarting must not spend again on its own.
        assert len(server.requests) == billing_requests


def test_long_objective_spill_and_tamper_detection(tmp_path: Path) -> None:
    """Long objectives (>4000 chars, multiline, Unicode) spill, load once and detect tampering.

    The consumer path is the registered form descriptor: its `load_action` is dispatched
    through the generic kernel command route, and the full text must come back exactly once.
    """
    # No replies: the driven turn fails fast; this test is about the objective transport.
    replies: list[dict[str, object]] = []
    with _loopback_server(replies) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        session_id = kernel.create_session().session_id

        huge_text = ("📌 Complete refactoring with boundary constraints: 严格执行。\n" * 120) + "End."
        assert len(huge_text) > 4000

        res_set = _invoke(kernel, session_id, "goal.set", {"objective": huge_text})
        assert res_set.status == "ok"
        ref = res_set.data["goal"]["objective_file_ref"]
        assert ref is not None
        spilled_file = tmp_path / "workspace" / ref
        assert spilled_file.is_file()
        assert spilled_file.read_text(encoding="utf-8") == huge_text

        # The registered form descriptor is the consumer entry point for editing.
        form = next(view for view in kernel.plugin_views(session_id).views if view.name == "goal.form")
        assert "objective" not in form.data["initial_values"]  # spilled text is never prefilled
        load_action = form.data["load_action"]

        loaded = _invoke(kernel, session_id, load_action, {})
        assert loaded.status == "ok"
        assert loaded.data["objective"] == huge_text
        assert loaded.output is None  # the full text crosses the wire once

        # Tampering with the spilled file must fail visibly through the same loader.
        spilled_file.write_text(huge_text + " MALICIOUS EDIT", encoding="utf-8")
        tampered = _invoke(kernel, session_id, load_action, {})
        assert tampered.status == "error"
        assert tampered.error


def test_second_plugin_namespace_and_budget_parity(tmp_path: Path) -> None:
    """Ensure Goal plugin and an independent second plugin coexist without cross-contamination."""
    second_root = tmp_path / "second-plugin"
    second_plugin_cfg = _second_plugin(second_root)

    # No replies: the driven turn fails fast and the goal stays active for the parity check.
    replies: list[dict[str, object]] = []
    with _loopback_server(replies) as server:
        kernel = _kernel_with_loopback(tmp_path, server, extra_plugins=second_plugin_cfg)
        session_id = kernel.create_session().session_id

        # Goal command sets goal state
        _invoke(kernel, session_id, "goal.set", {"objective": "Goal objective", "token_budget": 2000})

        # Second plugin command sets second plugin state
        res_second = _invoke(kernel, session_id, "second_cmd", {})
        assert res_second.status == "ok"

        # Goal state intact
        res_goal = _invoke(kernel, session_id, "goal", {})
        assert res_goal.data["goal"]["objective"] == "Goal objective"
        assert res_goal.data["goal"]["token_budget"] == 2000


def test_native_blocked_batch_counts_one_audit_turn_and_three_distinct_turns_block(tmp_path: Path) -> None:
    """A real native batch must not qualify as three blocked turns.

    One provider response carries three distinct `update_goal(status="blocked")` tool IDs in a
    single native batch: the audit must count that as ONE goal turn. Two further fresh
    provider turns then reach the third distinct turn and mark the goal `blocked`, with no
    fourth provider request.
    """
    batch_calls = [
        {
            "id": f"call-blocked-{index}",
            "type": "function",
            "function": {"name": "update_goal", "arguments": json.dumps({"status": "blocked"})},
        }
        for index in (1, 2, 3)
    ]
    replies = [
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": batch_calls},
            finish="tool_calls",
            usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
        ),
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [batch_calls[0] | {"id": "call-blocked-4"}]},
            finish="tool_calls",
            usage={"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
        ),
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [batch_calls[0] | {"id": "call-blocked-5"}]},
            finish="tool_calls",
            usage={"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        session_id = kernel.create_session().session_id

        # Idle start: the plugin enqueues work and the kernel drives it without a user prompt.
        res_set = _invoke(kernel, session_id, "goal.set", {"objective": "Investigate the blocked impasse"})
        assert res_set.status == "ok"

        goal = _invoke(kernel, session_id, "goal", {}).data["goal"]
        assert goal["status"] == "blocked"
        assert goal["blocked_turns_count"] == 3
        # Three distinct logical turns: one batch turn plus two later turns, no fourth request.
        assert len(server.requests) == 3


def _append_seeded_events(kernel: Kernel, session_id: str, drafts: tuple[EventDraft, ...]) -> SessionRecord:
    """Append SEEDED historical source rows using the real durable commit path."""
    session = kernel.store.session(session_id)
    return kernel.store.commit_staged(
        session_id,
        expected_sequence=session.last_event_sequence,
        expected_run_id=session.run_id,
        run_id=session.run_id or uuid4().hex,
        revision_id=kernel.revision_id,
        events=drafts,
        status=session.status,
    )


def _seed_known_zero_dispatch(kernel: Kernel, session_id: str) -> int:
    """Seed an old dispatch (intent + known-zero model turn) as SEEDED historical rows.

    Returns the dispatch sequence. The one-shot wire cannot produce history, so the rows are
    labelled seeded; every fact afterwards is derived by core from these stored rows.
    """
    logical_turn = uuid4().hex
    dispatch_id = uuid4().hex
    _append_seeded_events(
        kernel,
        session_id,
        (
            EventDraft(
                "kernel.provider_intent",
                "kernel",
                "kernel",
                {
                    "attempt": 0,
                    "provider": "endpoint",
                    "model": "exact-model",
                    "dispatch_id": dispatch_id,
                    "start_time_ms": int(time.time() * 1000),
                    "logical_turn": logical_turn,
                },
            ),
        ),
    )
    dispatch_sequence = kernel.store.session(session_id).last_event_sequence
    turn = ProviderTurnResult(
        output="seeded historical turn",
        usage=ProviderTokenUsage(input_tokens=0, output_tokens=0, cache_read_tokens=0, uncached_input_tokens=0),
        done_reason="stop",
        finish_reason_reported=True,
    )
    _append_seeded_events(
        kernel,
        session_id,
        (
            EventDraft(
                "kernel.model_turn",
                "provider",
                "model",
                {
                    "result": encode_provider_result(turn),
                    "dispatch_sequence": dispatch_sequence,
                    "dispatch_id": dispatch_id,
                    "elapsed_ms": 0,
                    "provider": "endpoint",
                    "model": "exact-model",
                },
            ),
        ),
    )
    # Canonical recovery: deliver the fact for the settled dispatch and persist its receipt.
    kernel._replay_pending_observers()
    return dispatch_sequence


def _seed_late_unknown_correction(kernel: Kernel, session_id: str, dispatch_sequence: int) -> None:
    """Append a SEEDED late `provider_interrupted` row for the ORIGINAL old dispatch.

    Provider, model and dispatch id are read from the stored intent row, so the correction
    carries the same identity as the dispatch it corrects; only the late report itself is
    seeded, because a one-shot wire cannot emit it naturally.
    """
    intent = next(ev for ev in kernel.store.events(session_id) if ev.sequence == dispatch_sequence and ev.event_type == "kernel.provider_intent")
    _append_seeded_events(
        kernel,
        session_id,
        (
            EventDraft(
                "kernel.provider_interrupted",
                "kernel",
                "kernel",
                {
                    "error": "seeded historical billing correction",
                    "unknown_usage": True,
                    "dispatch_sequence": dispatch_sequence,
                    "dispatch_id": intent.payload["dispatch_id"],
                    "elapsed_ms": 0,
                    "provider": intent.payload["provider"],
                    "model": intent.payload["model"],
                },
            ),
        ),
    )
    kernel._replay_pending_observers()


def test_late_closed_window_unknown_hard_blocks_same_baseline_and_uncap_frees_chat(tmp_path: Path) -> None:
    """Financial consumer boundary for a LATE closed-window unknown correction.

    Auto-drain is disabled so `goal.resume` rotates the window without immediately running a
    provider turn; the rotation itself is the subject. The old dispatch is SEEDED as durable
    source rows (intent + known-zero model turn) and later corrected by a SEEDED late
    `provider_interrupted` row; core derives both facts from stored rows and the real host
    observer applies them (no service mocks, no invented metadata, no paid dispatch).
    """
    replies = [
        openai_response(
            message={"role": "assistant", "content": "ordinary answer"},
            finish="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        kernel.auto_drain = False  # rotate the window without driving a provider turn
        session_id = kernel.create_session().session_id

        _invoke(kernel, session_id, "goal.set", {"objective": "Budgeted work", "token_budget": 5000})
        # Seeded historical dispatch inside the first window, with a known-zero report.
        old_dispatch = _seed_known_zero_dispatch(kernel, session_id)
        old_usage = kernel.usage(session_id)
        assert old_usage.has_unknown_usage is False
        assert old_usage.input_tokens == 0

        _invoke(kernel, session_id, "goal.pause", {})
        closed = _invoke(kernel, session_id, "goal", {}).data["goal"]["active_windows"][0]
        assert closed["end_sequence"] is not None
        assert closed["end_sequence"] >= old_dispatch  # the seeded dispatch is inside the old window

        resume_res = _invoke(kernel, session_id, "goal.resume", {})
        assert resume_res.status == "ok"
        open_baseline = resume_res.data["goal"]["active_windows"][-1]["start_sequence"]
        assert open_baseline > closed["end_sequence"]
        budget_before = kernel.store.owner_budget(session_id, "goal")
        assert budget_before is not None
        assert budget_before.since_sequence == open_baseline
        assert budget_before.max_total_tokens == 5000
        assert len(server.requests) == 0  # nothing billed yet

        # Late SEEDED correction for the OLD dispatch: core usage for that window is unknown.
        _seed_late_unknown_correction(kernel, session_id, old_dispatch)
        corrected = kernel.usage(session_id, since_sequence=closed["start_sequence"], until_sequence=closed["end_sequence"])
        assert corrected.has_unknown_usage is True
        assert corrected.input_tokens is None

        # Same current baseline, hard zero allowance: the old unknown bill cannot be ignored.
        goal_after = _invoke(kernel, session_id, "goal", {}).data["goal"]
        assert goal_after["status"] == "usage-limited"
        budget_after = kernel.store.owner_budget(session_id, "goal")
        assert budget_after is not None
        assert budget_after.since_sequence == open_baseline
        assert budget_after.max_total_tokens == 0

        # Ordinary non-goal chat is refused at admission: no provider request is made.
        refused = kernel.run("ordinary user turn", session_id=session_id)
        assert refused.status != "completed"
        assert refused.stop_cause == "limit_reached"
        assert len(server.requests) == 0

        # Explicit uncap removes the owner's constraint BEFORE any resume.
        uncap = _invoke(kernel, session_id, "goal.edit", {"token_budget": None})
        assert uncap.status == "ok"
        assert kernel.store.owner_budget(session_id, "goal") is None

        # A limit-stopped session must be explicitly resumed; with the constraint gone the
        # interrupted ordinary run really completes and bills exactly once.
        allowed = kernel.resume(session_id)
        assert allowed.status == "completed"
        assert len(server.requests) == 1

        # A late correction while paused must not re-arm any constraint.
        _invoke(kernel, session_id, "goal.pause", {})
        _seed_late_unknown_correction(kernel, session_id, old_dispatch)
        assert kernel.store.owner_budget(session_id, "goal") is None
        paused_goal = _invoke(kernel, session_id, "goal", {}).data["goal"]
        assert paused_goal["status"] == "paused"
        # Release the workspace lease (the host child inherits it) before reopening.
        kernel.close()

    # Startup must not double count or re-deliver the seeded receipts.
    with _loopback_server([]) as restart_server:
        kernel2 = _kernel_with_loopback(tmp_path, restart_server)
        restarted = kernel2.usage(session_id)
        assert restarted.has_unknown_usage is True
        assert restarted.input_tokens is None
        assert len(restart_server.requests) == 0


def test_deactivation_pure_rpc_pauses_live_goal_in_the_real_host(tmp_path: Path) -> None:
    """The real plugin host runs the goal's pure deactivation transform over its RPC boundary.

    This is the transform the kernel calls when the owner is disabled: state in, paused copy
    out, with no service, lease, clock or provider traffic. Auto-drain is off so the objective is
    never dispatched, and the durable row is only read afterwards: a pure transform writes nothing.
    """
    with _loopback_server([]) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        kernel.auto_drain = False
        session_id = kernel.create_session().session_id
        _invoke(kernel, session_id, "goal.set", {"objective": "Pure RPC deactivation", "token_budget": 4000})

        stored, version = kernel.store.plugin_state("goal", "session", session_id)
        assert stored is not None
        assert stored["status"] == "active"
        open_window = stored["active_windows"][-1]
        assert open_window["end_sequence"] is None
        tip = kernel.store.session(session_id).last_event_sequence

        prepared = kernel.host.prepare_deactivate_owner_state(
            "goal",
            stored,
            scope="session",
            scope_key=session_id,
            tip_sequence=tip,
        )

        assert prepared["status"] == "paused"
        assert prepared["goal_id"] == stored["goal_id"]
        assert prepared["objective"] == stored["objective"]
        assert prepared["token_budget"] == 4000
        assert prepared["tokens_used"] == stored["tokens_used"]
        assert prepared["created_at"] == stored["created_at"]
        assert prepared["updated_at"] == stored["updated_at"]
        assert prepared["active_windows"][-1] == {"start_sequence": open_window["start_sequence"], "end_sequence": tip}

        # The transform is pure: the durable row is untouched and no provider was contacted.
        assert len(server.requests) == 0
        still_stored, still_version = kernel.store.plugin_state("goal", "session", session_id)
        assert still_version == version
        assert still_stored == stored

        # An open window without the owning session's tip is refused at the boundary, never guessed.
        with pytest.raises(RuntimeError, match="event tip"):
            kernel.host.prepare_deactivate_owner_state(
                "goal",
                stored,
                scope="workspace",
                scope_key=str(kernel.workspace),
                tip_sequence=None,
            )

        # The scope/tip contract and a stale tip are refused before any copy can be written.
        with pytest.raises(RuntimeError, match="tip"):
            kernel.host.prepare_deactivate_owner_state("goal", stored, scope="session", scope_key=session_id, tip_sequence=None)
        with pytest.raises(RuntimeError, match="tip"):
            kernel.host.prepare_deactivate_owner_state("goal", stored, scope="workspace", scope_key=str(kernel.workspace), tip_sequence=4)
        with pytest.raises(RuntimeError, match="earlier point"):
            kernel.host.prepare_deactivate_owner_state("goal", stored, scope="session", scope_key=session_id, tip_sequence=0)
        still_stored, still_version = kernel.store.plugin_state("goal", "session", session_id)
        assert still_version == version
        assert still_stored == stored  # a refused deactivation changes nothing


def test_same_session_disable_and_reenable_keeps_paused_goal(tmp_path: Path) -> None:
    """Disabling the owner pauses the live goal; only an explicit resume arms work again.

    The disable is a real capability activation (`kernel.reload`) on the SAME session: the pure
    transform closes the open window at the session's own event tip, and the kernel clears the
    owner's budget in that commit. Ordinary chat then completes with no goal context and a single
    billed turn. After the owner is re-enabled the goal is still paused with its identity, its
    counters and its timestamps, nothing dispatches on its own, and `goal.resume` alone opens a
    window and arms the cap. The cap is deliberately smaller than the next dispatch's bill, and
    the loopback provider reads the kernel's durable owner budget before it answers that first
    resumed request: the resume commit armed it BEFORE the bill, the ordinary turn is not charged
    to the goal, and the run takes no concluding turn an uncapped run would take.
    """
    replies = [
        # 1: the ordinary (goal-disabled) turn, billed to nobody.
        openai_response(
            message={"role": "assistant", "content": "ordinary answer"},
            finish="stop",
            usage={"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
        ),
        # 2: the resumed goal's first turn (25 tokens) — it would complete the goal if uncapped.
        openai_response(
            message={
                "role": "assistant",
                "content": "Goal work done.",
                "tool_calls": [
                    {
                        "id": "call-complete-1",
                        "type": "function",
                        "function": {"name": "update_goal", "arguments": json.dumps({"status": "complete"})},
                    }
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
        # 3: only reached when the cap failed to bound the run after turn 2.
        openai_response(
            message={"role": "assistant", "content": "Goal marked complete."},
            finish="stop",
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        ),
    ]
    # Read while each provider request is in flight, BEFORE any byte of its reply is written:
    # the durable kernel budget must already be armed at that moment, not staged after the bill.
    observed_owner_budgets: list[tuple[int | None, int | None]] = []
    kernel_holder: list[Kernel] = []
    session_holder: list[str] = []

    def _observe_owner_budget(_request_number: int) -> None:
        budget = kernel_holder[0].store.owner_budget(session_holder[0], "goal")
        observed_owner_budgets.append((None if budget is None else budget.since_sequence, None if budget is None else budget.max_total_tokens))

    with _loopback_server(replies, lambda loopback_replies: _InFlightLoopbackServer(loopback_replies, _observe_owner_budget)) as server:
        kernel = _kernel_with_loopback(tmp_path, server)
        kernel_holder.append(kernel)
        kernel.auto_drain = False  # nothing dispatches unless this test asks for it
        session_id = kernel.create_session().session_id
        session_holder.append(session_id)
        config_path = tmp_path / "workspace" / "voidcode.json"

        _invoke(kernel, session_id, "goal.set", {"objective": "Deactivation keeps this identity", "token_budget": 10})
        before = _invoke(kernel, session_id, "goal", {}).data["goal"]
        assert before["status"] == "active"
        assert before["tokens_used"] == 0

        # --- disable the owner without ending the session ---
        tip = kernel.store.session(session_id).last_event_sequence
        _set_goal_enabled(config_path, False)
        disabled = _invoke(kernel, session_id, "kernel.reload", {})
        assert disabled.status == "ok", disabled.error
        assert tuple(disabled.data["deactivated_owners"]) == ("goal",)
        assert len(server.requests) == 0  # deactivating a goal bills nothing

        paused, _version = kernel.store.plugin_state("goal", "session", session_id)
        assert paused is not None
        assert paused["status"] == "paused"
        assert paused["active_windows"][-1]["end_sequence"] == tip
        assert paused["goal_id"] == before["goal_id"]
        assert paused["created_at"] == before["created_at"]
        assert paused["updated_at"] == before["updated_at"]
        assert paused["token_budget"] == before["token_budget"]
        assert paused["tokens_used"] == 0
        assert kernel.store.owner_budget(session_id, "goal") is None

        # The disabled owner is gone from the session: no command, no context, one ordinary bill.
        with pytest.raises(ValueError, match="Unknown command"):
            _invoke(kernel, session_id, "goal", {})
        ordinary = kernel.run("ordinary user turn", session_id=session_id)
        assert ordinary.status == "completed"
        assert len(server.requests) == 1
        assert "Deactivation keeps this identity" not in json.dumps(server.requests[0])

        # --- re-enable the owner on the same session ---
        _set_goal_enabled(config_path, True)
        enabled = _invoke(kernel, session_id, "kernel.reload", {})
        assert enabled.status == "ok", enabled.error
        assert len(server.requests) == 1  # re-enabling alone dispatches nothing

        after = _invoke(kernel, session_id, "goal", {}).data["goal"]
        assert after["status"] == "paused"
        assert after["goal_id"] == before["goal_id"]
        assert after["created_at"] == before["created_at"]
        assert after["updated_at"] == before["updated_at"]
        assert after["token_budget"] == 10
        assert after["tokens_used"] == 0  # the ordinary turn was never charged to the goal
        assert after["active_windows"] == paused["active_windows"]
        assert kernel.store.owner_budget(session_id, "goal") is None

        # The explicit resume alone must drive the goal: auto-drain is armed first, no prompt.
        kernel.auto_drain = True
        resume = _invoke(kernel, session_id, "goal.resume", {})
        assert resume.status == "ok", resume.error
        assert resume.data["goal"]["status"] == "active"
        assert resume.data["goal"]["window_allowance"] == 10
        baseline = resume.data["goal"]["active_windows"][-1]["start_sequence"]

        # The cap armed by the resume commit was live for the very first resumed dispatch: the
        # in-flight probe above read the durable owner budget on the kernel's own store before the
        # provider answered, and the observer staged an over-cap bill against that exact allowance.
        # An uncapped run would still take the concluding provider turn (reply 3); the armed cap
        # stops the run before it, which is what the request count distinguishes.
        resumed_goal = _invoke(kernel, session_id, "goal", {}).data["goal"]
        assert server.request_count == 2
        assert len(server.requests) == 2
        assert "Deactivation keeps this identity" in json.dumps(server.requests[1])
        assert observed_owner_budgets == [(None, None), (baseline, 10)]
        over_cap = [event for event in kernel.store.events(session_id) if event.event_type == "plugin.goal.goal_budget_limited"]
        assert over_cap, "the resume-armed cap must bound the first resumed dispatch"
        assert over_cap[-1].payload["window_tokens_used"] == 25
        assert over_cap[-1].payload["window_allowance"] == 10
        assert resumed_goal["status"] == "complete"
        assert resumed_goal["tokens_used"] == 25  # the new window only, not the ordinary 18
        assert resumed_goal["window_tokens_used"] == 25
        assert resumed_goal["input_tokens"] == 20
        assert resumed_goal["output_tokens"] == 5
        assert resumed_goal["has_unknown_usage"] is False
        assert resumed_goal["goal_id"] == before["goal_id"]
        assert resumed_goal["created_at"] == before["created_at"]
        assert resumed_goal["token_budget"] == 10
        assert resumed_goal["active_windows"][-1]["start_sequence"] == baseline
        assert resumed_goal["active_windows"][-1]["end_sequence"] is not None
        assert kernel.store.owner_budget(session_id, "goal") is None  # completion releases the cap
