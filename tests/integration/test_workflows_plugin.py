"""End-to-end workflows: real core, loopback provider, durable parallel review."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.core.engine import Core

_REVIEW_SCRIPT = '''"""Parallel review then summary: the first configured example workflow."""


async def run(ctx, task):
    async with asyncio.TaskGroup() as group:
        first = group.create_task(ctx.agent("review", f"Review A: {task}", key="review-a"))
        second = group.create_task(ctx.agent("review", f"Review B: {task}", key="review-b"))
    return f"combined: {first.result()} | {second.result()}"


import asyncio  # noqa: E402 - example keeps the entry first
'''


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _core(tmp_path: Path, server: OpenAILoopbackServer, script: str = _REVIEW_SCRIPT, *, approval_mode: str = "yolo") -> Core:
    workspace = tmp_path / "workspace"
    flows = workspace / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    (flows / "parallel_review.py").write_text(script, encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS}
    plugins["providers"] = {"enabled": True}
    plugins["delegation"] = {"enabled": True}
    plugins["exec"] = {"enabled": True}
    plugins["workflows"] = {"enabled": True, "config": {"workflows": {"parallel_review": "flows/parallel_review.py"}}}
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/test-model"},
                "policy": {"approval_mode": approval_mode},
                "providers": {"endpoint": {"api_key": "loopback-test-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": plugins,
            }
        ),
        encoding="utf-8",
    )
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]):
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def _reply(text: str) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": text}, finish="stop")


def _tool_call_reply(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return openai_response(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
        },
        finish="tool_calls",
    )


def test_model_tool_start_receives_terminal_summary_as_followup(tmp_path: Path) -> None:
    replies = [
        _tool_call_reply("workflow_run", {"workflow": "parallel_review", "task": "diff 7"}),
        _reply("started"),
        _reply("A done"),
        _reply("B done"),
        _reply("noticed the summary"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id
        result = core.run("start the parallel review workflow", session_id=session_id)
        assert result.status == "completed", result.error
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        # The terminal notice either drains automatically or waits for an explicit drain;
        # either way one follow-up turn must observe the bounded summary.
        core.drain_continuations(session_id, limit=10)
        terminal = [request for request in server.requests if "workflow_terminal" in json.dumps(request.get("messages", []))]
        assert len(terminal) == 1, f"expected one terminal-notice turn, saw {len(server.requests)} turns"
        assert "combined: A done | B done" in json.dumps(terminal[0])
        assert "not instructions" in json.dumps(terminal[0])


def test_parallel_review_completes_with_combined_result(tmp_path: Path) -> None:
    with _loopback_server([_reply("A says ship it."), _reply("B says fix one thing.")]) as server:
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "diff 42"})
        assert started.status == "ok", started
        assert started.data is not None
        run_id = str(started.data["run_id"])
        action_id = str(started.data["action_id"])
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        status = _invoke(core, session_id, "workflow.status", {"run_id": run_id})
        assert status.status == "ok", status
        assert status.data is not None
        assert status.data["status"] == "completed", status.output
        assert status.data["action_id"] == action_id
        run_result = status.data["run_result"]  # type: ignore[index]
        assert "A says ship it." in run_result["text"] and "B says fix one thing." in run_result["text"]
        by_key = {node["key"]: node for node in status.data["nodes"]}  # type: ignore[index]
        assert set(by_key) == {"review-a", "review-b"}
        assert all(node["status"] == "completed" for node in by_key.values())
        assert all(node["agent"] == "review" for node in by_key.values())


_BLOCKING_SCRIPT = '''"""Two children that park on approval until the test cancels them."""


async def run(ctx, task):
    async with asyncio.TaskGroup() as group:
        group.create_task(ctx.agent("execute", f"Explore A: {task}", key="explore-a"))
        group.create_task(ctx.agent("execute", f"Explore B: {task}", key="explore-b"))
    return "unreachable"


import asyncio  # noqa: E402 - example keeps the entry first
'''


def _waiting_reply(text: str = "still working") -> dict[str, object]:
    return openai_response(
        message={
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": "call-needs-approval",
                    "type": "function",
                    "function": {"name": "shell_exec", "arguments": json.dumps({"command": "echo parked"})},
                }
            ],
        },
        finish="tool_calls",
    )


class _TwoFlightBarrier:
    """Hold the first two provider turns open until both are provably in flight."""

    def __init__(self, first: dict[str, object], second: dict[str, object]) -> None:
        from threading import Lock

        self._first = first
        self._second = second
        self.arrived = Event()
        self.release = Event()
        self._count = 0
        self._guard = Lock()

    def __call__(self, payload: dict[str, object], index: int) -> dict[str, object] | None:
        _ = payload
        with self._guard:
            self._count += 1
            if self._count == 2:
                self.arrived.set()
        assert self.release.wait(timeout=60.0), "barrier never released"
        return self._first if index == 1 else self._second if index == 2 else None


def _confirm_cancel(core: Core, session_id: str, run_id: str) -> None:
    cancelled = _invoke(core, session_id, "workflow.cancel", {"run_id": run_id})
    assert cancelled.status in ("ok", "pending_confirmation"), cancelled
    if cancelled.status == "pending_confirmation":
        assert cancelled.confirmation is not None
        confirmed = core.confirm_action(session_id, cancelled.confirmation.request_id, "allow", expected_revision=core.command_catalog().revision_id)
        assert confirmed.status == "ok", confirmed


def test_cancel_while_both_children_blocked_stops_everything(tmp_path: Path) -> None:
    barrier = _TwoFlightBarrier(_reply("A late"), _reply("B late"))
    with _loopback_server([]) as server:
        server.before_reply = barrier
        core = _core(tmp_path, server, script=_BLOCKING_SCRIPT)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "deep dive"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        assert barrier.arrived.wait(timeout=60.0), "both children never reached the provider"
        _confirm_cancel(core, session_id, run_id)
        barrier.release.set()
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        status = _invoke(core, session_id, "workflow.status", {"run_id": run_id})
        assert status.status == "ok" and status.data is not None, status
        assert status.data["status"] == "cancelled", status.output
        assert len(status.data["nodes"]) == 2  # type: ignore[index]
        assert all(node["status"] == "cancelled" for node in status.data["nodes"])  # type: ignore[index]
        for node in status.data["nodes"]:  # type: ignore[index]
            assert node["child_session_id"] is not None
            child = core.session(node["child_session_id"])
            assert child.status in ("cancelled", "interrupted"), child.status
        driver = status.data["driver"]  # type: ignore[index]
        assert driver is not None and driver["status"] == "cancelled", driver


def test_pause_keeps_children_and_resume_completes_without_duplicates(tmp_path: Path) -> None:
    barrier = _TwoFlightBarrier(_reply("A says ship it."), _reply("B says fix one thing."))
    with _loopback_server([]) as server:
        server.before_reply = barrier
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "diff 42"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        assert barrier.arrived.wait(timeout=60.0), "both children never reached the provider"
        paused = _invoke(core, session_id, "workflow.pause", {"run_id": run_id})
        assert paused.status == "ok", paused
        # The barrier stays held: the old driver must stop while children are still
        # blocked at the provider, and resume must rejoin those same children.
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        parked = _invoke(core, session_id, "workflow.status", {"run_id": run_id})
        assert parked.data is not None and parked.data["status"] == "paused", parked.output  # type: ignore[index]
        children_before = sorted(node["child_session_id"] for node in parked.data["nodes"])  # type: ignore[index]
        assert len(children_before) == 2 and all(children_before)
        for child_id in children_before:
            assert core.session(child_id).status not in ("cancelled", "failed"), "pause must not stop children"
        resumed = _invoke(core, session_id, "workflow.resume", {"run_id": run_id})
        assert resumed.status == "ok", resumed
        barrier.release.set()
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        done = _invoke(core, session_id, "workflow.status", {"run_id": run_id})
        assert done.data is not None and done.data["status"] == "completed", done.output  # type: ignore[index]
        assert sorted(node["child_session_id"] for node in done.data["nodes"]) == children_before  # type: ignore[index]
        assert "A says ship it." in done.data["run_result"]["text"]  # type: ignore[index]
        child_turns = [request for request in server.requests if "[Durable continuation" not in json.dumps(request.get("messages", []))]
        assert len(child_turns) == 2, "no child may run twice"


def test_spawn_budget_exhaustion_fails_visibly(tmp_path: Path) -> None:
    many = "\n".join(f'        group.create_task(ctx.agent("review", f"node {i}: {{task}}", key="n{i}"))' for i in range(6))
    script = (
        '"""Six nodes against a quota of four."""\n\n\nasync def run(ctx, task):\n'
        f"    async with asyncio.TaskGroup() as group:\n{many}\n"
        '    return "unreachable"\n\n\nimport asyncio  # noqa: E402\n'
    )
    replies = [_reply(f"done {i}") for i in range(6)]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, script=script)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "flood"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        try:
            core.wait_background_actions(session_id, timeout_seconds=120.0)
        except RuntimeError:
            pass
        status = _invoke(core, session_id, "workflow.status", {"run_id": run_id})
        assert status.status == "ok" and status.data is not None, status
        assert status.data["status"] == "failed", status.output
        assert "budget exhausted" in (status.data["error"] or ""), status.output


def test_approval_park_then_resume_completes_without_new_child(tmp_path: Path) -> None:
    script = (
        '"""One node that needs one approval."""\n\n\nasync def run(ctx, task):\n'
        '    return await ctx.agent("execute", f"Run it: {task}", key="only")\n'
    )
    replies = [_waiting_reply("needs approval"), _reply("approved and done")]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, script=script, approval_mode="ask")
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "go"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        parked = _invoke(core, session_id, "workflow.status", {"run_id": run_id})
        assert parked.data is not None and parked.data["status"] == "parked", parked.output  # type: ignore[index]
        node = parked.data["nodes"][0]  # type: ignore[index]
        assert node["status"] == "waiting" and node["child_session_id"]
        child_id = node["child_session_id"]
        assert parked.data["error"] is not None and "parked" in parked.data["error"]  # type: ignore[index]
        approvals = [e for e in core.store.events(child_id) if e.event_type == "core.approval_requested"]
        assert len(approvals) == 1
        request_id = approvals[0].payload["request_id"]
        resumed_child = core.resume(child_id, approval_request_id=request_id, decision="allow")
        assert resumed_child.status == "completed", resumed_child.error
        resumed = _invoke(core, session_id, "workflow.resume", {"run_id": run_id})
        assert resumed.status == "ok", resumed
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        done = _invoke(core, session_id, "workflow.status", {"run_id": run_id})
        assert done.data is not None and done.data["status"] == "completed", done.output  # type: ignore[index]
        assert done.data["nodes"][0]["child_session_id"] == child_id  # type: ignore[index]
        assert done.data["nodes"][0]["status"] == "completed"  # type: ignore[index]
        assert "approved and done" in done.data["run_result"]["text"]  # type: ignore[index]
        child_turns = [request for request in server.requests if "[Durable continuation" not in json.dumps(request.get("messages", []))]
        assert len(child_turns) == 2, "tool call plus post-approval turn; resume only joined"


def test_run_snapshots_without_executing_and_changed_source_refuses_resume(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    script = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\nasync def run(ctx, task):\n    return 'ok'\n"
    with _loopback_server([]) as server:
        core = _core(tmp_path, server, script=script)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "t"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        paused = _invoke(core, session_id, "workflow.pause", {"run_id": run_id})
        assert paused.status == "ok", paused
        # Deterministic gate: no driver is alive after this, so any later marker
        # could only come from executing the changed source — which must not happen.
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        marker.unlink(missing_ok=True)
        (tmp_path / "workspace" / "flows" / "parallel_review.py").write_text(script + "# changed\n", encoding="utf-8")
        refused = _invoke(core, session_id, "workflow.resume", {"run_id": run_id})
        assert refused.status == "error", refused
        assert refused.error is not None and "source changed" in refused.error
        assert not marker.exists(), "a refused resume must not execute the changed source"


def test_status_pages_large_multibyte_node_result_without_corruption(tmp_path: Path) -> None:
    import hashlib

    full = "汉" * 9000
    expected_hash = hashlib.sha256(full.encode("utf-8")).hexdigest()
    script = '"""One node with a large reply."""\n\n\nasync def run(ctx, task):\n    return await ctx.agent("review", f"Say it: {task}", key="big")\n'
    with _loopback_server([_reply(full)]) as server:
        core = _core(tmp_path, server, script=script)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "t"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        pieces: list[str] = []
        offset = 0
        total = -1
        for _ in range(10):
            page = _invoke(core, session_id, "workflow.status", {"run_id": run_id, "result_key": "big", "result_offset": offset})
            assert page.status == "ok" and page.data is not None, page
            result = page.data["result"]  # type: ignore[index]
            total = result["total_bytes"]
            assert "\ufffd" not in result["text"]
            pieces.append(result["text"])
            offset = result["next_offset"]
            if offset >= total:
                break
        assert total == len(full.encode("utf-8"))
        assert hashlib.sha256("".join(pieces).encode("utf-8")).hexdigest() == expected_hash


def _open_configured(tmp_path: Path, tag: str, name: str, body: str) -> tuple[Core, str]:
    workspace = tmp_path / f"workspace-{tag}"
    flows = workspace / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    (flows / f"{name}.py").write_text(f'"""{name}."""\n\n\nasync def run(ctx, task):\n    return "{body}"\n', encoding="utf-8")
    state_dir = tmp_path / f"state-{tag}"
    state_dir.mkdir(parents=True, exist_ok=True)
    plugins: dict[str, object] = {plugin: {"enabled": False} for plugin in BUNDLED_PLUGIN_IDS}
    plugins["workflows"] = {"enabled": True, "config": {"workflows": {name: f"flows/{name}.py"}}}
    (workspace / "voidcode.json").write_text(
        json.dumps({"schema_version": 1, "policy": {"approval_mode": "yolo"}, "plugins": plugins}), encoding="utf-8"
    )
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core, core.create_session().session_id


def test_two_cores_keep_their_own_workflow_registries(tmp_path: Path) -> None:
    with _loopback_server([]):
        core_a, session_a = _open_configured(tmp_path, "a", "only_a", "result A")
        core_b, session_b = _open_configured(tmp_path, "b", "only_b", "result B")
        run_a = _invoke(core_a, session_a, "workflow.run", {"workflow": "only_a", "task": "t"})
        assert run_a.status == "ok", run_a
        run_b = _invoke(core_b, session_b, "workflow.run", {"workflow": "only_b", "task": "t"})
        assert run_b.status == "ok", run_b
        core_a.wait_background_actions(session_a, timeout_seconds=120.0)
        core_b.wait_background_actions(session_b, timeout_seconds=120.0)
        status_a = _invoke(core_a, session_a, "workflow.status", {"run_id": str(run_a.data["run_id"])})  # type: ignore[index]
        status_b = _invoke(core_b, session_b, "workflow.status", {"run_id": str(run_b.data["run_id"])})  # type: ignore[index]
        assert status_a.data is not None and status_a.data["run_result"]["text"] == "result A"  # type: ignore[index]
        assert status_b.data is not None and status_b.data["run_result"]["text"] == "result B"  # type: ignore[index]
        foreign = _invoke(core_a, session_a, "workflow.run", {"workflow": "only_b", "task": "t"})
        assert foreign.status == "error" and (foreign.error or "").strip() != "", foreign


def test_close_during_blocked_children_then_reopen_resume_rejoins(tmp_path: Path) -> None:
    barrier = _TwoFlightBarrier(_reply("A says ship it."), _reply("B says fix one thing."))
    with _loopback_server([_reply("A resumed done."), _reply("B resumed done.")]) as server:
        server.before_reply = barrier
        core = _core(tmp_path, server)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "diff 42"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        assert barrier.arrived.wait(timeout=60.0), "both children never reached the provider"
        workspace = tmp_path / "workspace"
        state_dir = tmp_path / "state"
        _CREATED_CORES.remove(core)
        core.close()
        barrier.release.set()
        reopened = open_core(workspace, state_dir=state_dir)
        _CREATED_CORES.append(reopened)
        status = _invoke(reopened, session_id, "workflow.status", {"run_id": run_id})
        assert status.data is not None and status.data["status"] == "running", status.output  # type: ignore[index]
        children = [node["child_session_id"] for node in status.data["nodes"]]  # type: ignore[index]
        assert len(children) == 2 and all(children)
        driver = status.data["driver"]  # type: ignore[index]
        assert driver is not None and driver["status"] not in ("pending", "running"), driver
        # The close orphaned the children mid-flight: authorize their unknown work
        # explicitly through the public child surface, then rejoin the same ids.
        for child_id in children:
            followup = reopened.resume(child_id)
            assert followup.status == "completed", followup.error
        resumed = _invoke(reopened, session_id, "workflow.resume", {"run_id": run_id})
        assert resumed.status == "ok", resumed
        reopened.wait_background_actions(session_id, timeout_seconds=120.0)
        done = _invoke(reopened, session_id, "workflow.status", {"run_id": run_id})
        assert done.data is not None and done.data["status"] == "completed", done.output  # type: ignore[index]
        assert sorted(node["child_session_id"] for node in done.data["nodes"]) == sorted(children)  # type: ignore[index]
        assert "A resumed done." in done.data["run_result"]["text"]  # type: ignore[index]
        assert "B resumed done." in done.data["run_result"]["text"]  # type: ignore[index]


def test_status_rejects_split_character_offset_on_both_paths(tmp_path: Path) -> None:
    script = (
        '"""One small node and one large node."""\n\n\nasync def run(ctx, task):\n'
        '    first = await ctx.agent("review", "small", key="small")\n'
        '    second = await ctx.agent("review", f"large: {task}", key="large")\n'
        '    return f"{first}/{second[:8]}"\n'
    )
    with _loopback_server([_reply("中文"), _reply("汉" * 9000)]) as server:
        core = _core(tmp_path, server, script=script)
        session_id = core.create_session().session_id
        started = _invoke(core, session_id, "workflow.run", {"workflow": "parallel_review", "task": "t"})
        assert started.status == "ok", started
        run_id = str(started.data["run_id"])  # type: ignore[index]
        core.wait_background_actions(session_id, timeout_seconds=120.0)
        for key in ("small", "large"):
            page = _invoke(core, session_id, "workflow.status", {"run_id": run_id, "result_key": key, "result_offset": 0})
            assert page.status == "ok" and page.data is not None, page
            total = page.data["result"]["total_bytes"]  # type: ignore[index]
            assert total == 6 if key == "small" else total == len(("汉" * 9000).encode("utf-8"))
            split = _invoke(core, session_id, "workflow.status", {"run_id": run_id, "result_key": key, "result_offset": 1})
            assert split.status == "error", split
            assert split.error is not None and split.error.strip() != ""
        # Chained next_offset paging still reassembles the large node exactly.
        import hashlib

        pieces: list[str] = []
        offset, total = 0, -1
        for _ in range(10):
            page = _invoke(core, session_id, "workflow.status", {"run_id": run_id, "result_key": "large", "result_offset": offset})
            assert page.status == "ok" and page.data is not None, page
            result = page.data["result"]  # type: ignore[index]
            total = result["total_bytes"]
            pieces.append(result["text"])
            offset = result["next_offset"]
            if offset >= total:
                break
        assert hashlib.sha256("".join(pieces).encode("utf-8")).hexdigest() == hashlib.sha256(("汉" * 9000).encode("utf-8")).hexdigest()
