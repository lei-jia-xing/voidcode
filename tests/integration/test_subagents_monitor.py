"""Monitor: read-only browsing plus fenced subtree cancellation over real nested children."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.events import child_completion_notice_id
from voidcode.core.history_renderer import iter_event_blocks
from voidcode.sdk.child_schema import CHILD_PROMPT_MAX_BYTES
from voidcode.sdk.views import ChoicesViewData, MarkdownViewData, choices_dispatch

_SUBAGENTS_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "subagents"


def _turn(*, output: str | None = None) -> dict[str, object]:
    return openai_response(message={"role": "assistant", "content": output, "tool_calls": []}, finish="stop")


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _open(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo"},
        "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, "subagents": {"path": str(_SUBAGENTS_ROOT), "enabled": True}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _wait_for(predicate: Callable[[], bool], *, what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _continuation_status(core: Core, session_id: str, continuation_id: str) -> str | None:
    for record in (
        *core.store.pending_continuations(session_id, limit=100),
        *core.store.claimed_continuations(session_id, limit=100),
    ):
        if record.continuation_id == continuation_id:
            return record.status
    return None


def _request_texts(payload: Mapping[str, object]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for entry in messages:
        if isinstance(entry, dict):
            parts.append(json.dumps(entry.get("content"), ensure_ascii=False))
    return "\n".join(parts)


@contextmanager
def _gated_markers(server: OpenAILoopbackServer, markers: list[str], *, release: threading.Event) -> Iterator[dict[str, threading.Event]]:
    """Hold provider replies until the test releases; entry keyed by child prompt marker.

    ``before_reply`` is global, so each gate fires only when the request body
    carries its own marker. Every held request is released together: the
    caller proves the settled state after release.
    """
    entered = {marker: threading.Event() for marker in markers}

    def _hold(payload: dict[str, object], index: int) -> dict[str, object] | None:
        _ = index
        text = _request_texts(payload)
        for marker, event in entered.items():
            if marker in text:
                event.set()
                assert release.wait(timeout=30), "gated provider request never released"
                return None
        return None

    server.before_reply = _hold
    try:
        yield entered
    finally:
        server.before_reply = None


def test_monitor_browsing_consumes_no_notice_and_stays_descendant_bound(tmp_path: Path) -> None:
    replies = [_turn(output="child answer"), _turn(output="grandchild answer")]
    with _server(replies) as server:
        release = threading.Event()
        with _gated_markers(server, ["NOTICE-CHILD", "NOTICE-GRAND"], release=release) as entered:
            core = _open(tmp_path, server)
            core.auto_drain = False
            core.create_session("root")
            core.create_session("sibling")
            rev = core.command_catalog().revision_id
            empty = core.command_input("root", "subagents.monitor", expected_revision=rev)
            assert empty.presentation.view_type == "choices"
            assert ChoicesViewData.from_payload(empty.presentation.data).options == ()

            child = core.spawn_child("root", "child work NOTICE-CHILD", notify_on_completion=True)
            assert entered["NOTICE-CHILD"].wait(timeout=30), "child never reached the provider gate"
            grandchild = core.spawn_child(child, "grandchild work NOTICE-GRAND", notify_on_completion=False)
            assert entered["NOTICE-GRAND"].wait(timeout=30), "grandchild never reached the provider gate"
            release.set()
            _wait_for(lambda: core.session(child).status == "completed", what="the child terminal commit")
            _wait_for(
                lambda: any(event.event_type == "core.child_finished" for event in core.store.events("root")),
                what="the child terminal fact",
            )
            run_id = core.session(child).run_id
            assert run_id is not None
            notice_id = child_completion_notice_id("root", child, run_id)
            assert _continuation_status(core, "root", notice_id) == "pending"

            roster = core.command_input("root", "subagents.monitor", expected_revision=rev)
            choices = ChoicesViewData.from_payload(roster.presentation.data)
            options = choices.options
            assert len(options) == 2
            assert options[0].label.startswith("completed") and not options[0].label.startswith("  ")
            assert options[1].label.startswith("  completed")
            action, arguments = choices_dispatch(choices, options[1].id)
            detail = core.command_input("root", action, arguments=arguments, expected_revision=rev)
            assert f"session {grandchild}\n" in MarkdownViewData.from_payload(detail.presentation.data).content
            again = core.command_input("root", action, arguments=arguments, expected_revision=rev)
            assert again.presentation.view_type == "markdown"
            # Browsing twice leaves the pending notice durably pending: no acknowledge path ran.
            assert _continuation_status(core, "root", notice_id) == "pending"
            sibling_view = core.command_input("sibling", "subagents.monitor", expected_revision=rev)
            assert ChoicesViewData.from_payload(sibling_view.presentation.data).options == ()
            with pytest.raises(ValueError, match="unknown subagent"):
                core.command_input("sibling", "subagents.monitor", arguments={"target_session_id": child}, expected_revision=rev)


def test_monitor_detail_paging_reaches_every_marker_exactly_once(tmp_path: Path) -> None:
    markers = [f'marker-{index:02d} “ünicode” "quoted" \\u0001' for index in range(12)]
    # Sized between the 40 KiB history page (still forces multi-page output: the
    # marker rides both the call-args and result blocks, ~76 KiB of history) and the
    # 50 KiB bounded tool-result commit ceiling (commits verbatim): 38,004 bytes of
    # marker, 44,211 bytes encoded with the JSON envelope.
    big = "BIG-" + '“引用”"q"\\x01' * 2000
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{index}",
                        "type": "function",
                        "function": {"name": "marker_tool", "arguments": json.dumps({"marker": marker})},
                    }
                    for index, marker in enumerate(markers[:6])
                ],
            },
            finish="tool_calls",
        ),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{index + 6}",
                        "type": "function",
                        "function": {"name": "marker_tool", "arguments": json.dumps({"marker": marker})},
                    }
                    for index, marker in enumerate(markers[6:])
                ]
                + [{"id": "call-big", "type": "function", "function": {"name": "marker_tool", "arguments": json.dumps({"marker": big})}}],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "done"}, finish="stop"),
    ]
    with _server(replies) as server:
        plugin_dir = tmp_path / "marker_plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "__init__.py").write_text(
            "from voidcode.sdk.plugin import Plugin\n"
            "from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess\n"
            "class MarkerTool:\n"
            "    definition = ToolDefinition(name='marker_tool', description='echo one marker',\n"
            "        input_schema={'type': 'object', 'properties': {'marker': {'type': 'string'}},\n"
            "        'required': ['marker'], 'additionalProperties': False},\n"
            "        effects=frozenset({ToolEffect.READ}))\n"
            "    def invoke(self, call, *, context, services):\n"
            "        return ToolSuccess(call.tool_name, TextOutput(str(call.arguments['marker'])))\n"
            "def create_plugin(config):\n"
            "    return Plugin(tools=(MarkerTool(),))\n",
            encoding="utf-8",
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        config = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": "yolo"},
            "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {
                "files": {"enabled": False},
                "subagents": {"path": str(_SUBAGENTS_ROOT), "enabled": True},
                "marker": {"path": str(plugin_dir), "enabled": True},
            },
        }
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        core = open_core(workspace, state_dir=tmp_path / "state")
        _CREATED.append(core)
        core.create_session("root")
        rev = core.command_catalog().revision_id
        child = core.spawn_child("root", "emit every marker through the tool", notify_on_completion=False)
        _wait_for(lambda: core.session(child).status == "completed", what="the marker run")
        expected = [*markers, big]
        # Ground truth: every committed readable block of the child session, in
        # durable order, from the same renderer the monitor pages through.
        committed = list(iter_event_blocks(core.store.events(child)))
        result_blocks = [block for block in committed if block.startswith("## tool marker_tool (result)")]
        assert len(result_blocks) == len(expected)
        footer = "\n\nMore history: use Next page."
        rebuilt = ""
        pages = 0
        cursor: dict[str, object] = {"target_session_id": child, "after_sequence": 0, "history_offset": 0, "limit": 5}
        for _ in range(30):
            page = core.command_input("root", "subagents.monitor", arguments=cursor, expected_revision=rev)
            view = MarkdownViewData.from_payload(page.presentation.data)
            pages += 1
            _head, separator, history = view.content.partition("\n\n")
            assert separator, "detail page must carry a history body after the header"
            history = history.removesuffix(footer)
            # Rejoin rule mirrors the renderer: a nonzero incoming offset means
            # this page continues the same block (no separator); otherwise the
            # canonical block separator joins it to the text so far.
            if cursor["history_offset"]:
                rebuilt += history
            else:
                rebuilt += ("\n\n" if rebuilt else "") + history
            actions = {action.label: action for action in view.actions}
            if "Next page" not in actions:
                break
            cursor = dict(actions["Next page"].arguments)
        else:
            raise AssertionError("Detail paging never terminated")
        assert pages >= 2, "oversized marker must force multiple pages"
        assert rebuilt == "\n\n".join(committed), "paged walk must reconstruct committed blocks verbatim"
        for marker in expected:
            assert rebuilt.count(marker) == 1, f"marker reachable exactly once: {marker[:32]!r}"


def test_cancel_deny_leaves_work_running_and_allow_requests_cancellation(tmp_path: Path) -> None:
    with _server([_turn(output="child answer")]) as server:
        release = threading.Event()
        with _gated_markers(server, ["DENY-MARKER"], release=release) as entered:
            core = _open(tmp_path, server)
            core.create_session("root")
            rev = core.command_catalog().revision_id
            child = core.spawn_child("root", "child work DENY-MARKER", notify_on_completion=False)
            assert entered["DENY-MARKER"].wait(timeout=30), "child never reached the provider gate"
            expected = core.session(child).run_id
            assert expected is not None

            denied = core.invoke_command(
                "root", "subagents.monitor.cancel", {"target_session_id": child, "expected_run_id": expected}, expected_revision=rev
            )
            assert denied.status == "pending_confirmation" and denied.confirmation is not None
            assert core.confirm_action("root", denied.confirmation.request_id, "deny", expected_revision=rev).status == "ok"
            assert core.session(child).status == "running"

            pending = core.invoke_command(
                "root", "subagents.monitor.cancel", {"target_session_id": child, "expected_run_id": expected}, expected_revision=rev
            )
            assert pending.status == "pending_confirmation" and pending.confirmation is not None
            receipt = core.confirm_action("root", pending.confirmation.request_id, "allow", expected_revision=rev)
            assert receipt.status == "ok", receipt.error
            assert receipt.data is not None and child in receipt.data["cancelled"]
            assert "requested" in (receipt.output or "")
            release.set()
            _wait_for(lambda: core.session(child).status == "interrupted", what="the owner-cancel settle")
            assert core.store.latest_stop_cause(child, core.session(child).run_id) == "owner_cancel"


def test_cancel_subtree_covers_nested_queued_work_and_refuses_siblings(tmp_path: Path) -> None:
    with _server([_turn(output="child answer"), _turn(output="grandchild answer"), _turn(output="sibling answer")]) as server:
        release = threading.Event()
        with _gated_markers(server, ["NEST-CHILD", "NEST-GRAND", "NEST-SIBLING"], release=release) as entered:
            core = _open(tmp_path, server)
            core.create_session("root")
            rev = core.command_catalog().revision_id
            child = core.spawn_child("root", "child work NEST-CHILD", notify_on_completion=False)
            assert entered["NEST-CHILD"].wait(timeout=30), "child never reached the provider gate"
            grandchild = core.spawn_child(child, "grandchild work NEST-GRAND", notify_on_completion=False)
            sibling = core.spawn_child("root", "sibling work NEST-SIBLING", notify_on_completion=False)
            assert entered["NEST-GRAND"].wait(timeout=30), "grandchild never reached the provider gate"
            assert entered["NEST-SIBLING"].wait(timeout=30), "sibling never reached the provider gate"
            expected = core.session(child).run_id
            assert expected is not None
            # A follow-up racing the cancel lands before the fence and must be disarmed, not leaked.
            core.send_child(child, grandchild, "racing follow-up", notify_on_completion=False)

            pending_receipt = core.invoke_command(
                "root",
                "subagents.monitor.cancel",
                {"target_session_id": child, "expected_run_id": expected},
                expected_revision=rev,
            )
            assert pending_receipt.status == "pending_confirmation", pending_receipt.error
            assert pending_receipt.confirmation is not None
            receipt = core.confirm_action("root", pending_receipt.confirmation.request_id, "allow", expected_revision=rev)
            assert receipt.status == "ok", receipt.error
            assert receipt.data is not None
            assert set(receipt.data["cancelled"] + receipt.data["disarmed_queued"] + receipt.data["claimed_reserved"]) >= {
                child,
                grandchild,
            }
            assert sibling not in receipt.data["cancelled"]
            assert core.store.pending_continuations(grandchild, limit=10) == ()
            release.set()
            _wait_for(lambda: core.session(grandchild).status == "interrupted", what="the grandchild settle")
            assert core.store.latest_stop_cause(grandchild, core.session(grandchild).run_id) == "owner_cancel"
            assert core.session("root").status == "idle"
            # A completed parent with a detached live descendant stays cancellable, and an
            # explicit follow-up afterwards is still a legal fresh run.
            follow = core.send_child(child, grandchild, "explicit new run after cancellation")
            assert follow.delivery in ("started", "queued")


def test_cancel_reports_a_run_claimed_after_census_distinctly(tmp_path: Path) -> None:
    with _server([_turn(output="child answer"), _turn(output="second answer")]) as server:
        release = threading.Event()
        with _gated_markers(server, ["RACE-CHILD", "RACE-NEWER"], release=release) as entered:
            core = _open(tmp_path, server)
            core.auto_drain = False
            core.create_session("root")
            rev = core.command_catalog().revision_id
            child = core.spawn_child("root", "child work RACE-CHILD", notify_on_completion=False)
            assert entered["RACE-CHILD"].wait(timeout=30), "child never reached the provider gate"
            expected = core.session(child).run_id
            assert expected is not None
            pending = core.invoke_command(
                "root", "subagents.monitor.cancel", {"target_session_id": child, "expected_run_id": expected}, expected_revision=rev
            )
            assert pending.status == "pending_confirmation" and pending.confirmation is not None
            # Gate the sweep at the bound signal: hold it while a real newer
            # run claims through the public run surface after the census. A
            # fenced send would be refused here, which is a separate invariant.
            at_signal = threading.Event()
            sweep_release = threading.Event()
            original_bound = core._delegation.core._cancel_session_run

            def _gated_bound(session_id: str, reason: str, **kwargs: object) -> bool:
                if session_id == child and not at_signal.is_set():
                    at_signal.set()
                    assert sweep_release.wait(timeout=30), "race test never released the sweep gate"
                return original_bound(session_id, reason, **kwargs)  # type: ignore[arg-type]

            core._delegation.core._cancel_session_run = _gated_bound  # type: ignore[method-assign]
            outcome: dict[str, object] = {}

            def _confirm() -> None:
                try:
                    outcome["result"] = core.confirm_action("root", pending.confirmation.request_id, "allow", expected_revision=rev)
                except Exception as exc:  # noqa: BLE001 - the race outcome is asserted below
                    outcome["error"] = exc

            worker = threading.Thread(target=_confirm, daemon=True)
            try:
                worker.start()
                assert at_signal.wait(timeout=30), "sweep never reached the target signal"
                # Release the provider so the first run settles, then start a
                # real newer run on its own gate while the sweep is held.
                release.set()
                _wait_for(lambda: core.session(child).status == "completed", what="the first run settle")
                newer_release = threading.Event()
                with _gated_markers(server, ["RACE-NEWER"], release=newer_release) as newer_entered:
                    runner: dict[str, object] = {}

                    def _run_newer() -> None:
                        try:
                            # The sweep holds the subtree fence while gated at the bound
                            # signal, so a fenced send would be refused; drive the racing
                            # run through the internal run surface with the parent's captured
                            # birth authorization, exactly as the child driver does.
                            runner["result"] = core._run(
                                "real newer run RACE-NEWER after census",
                                session_id=child,
                                prompt_max_bytes=CHILD_PROMPT_MAX_BYTES,
                                _root_id=core._delegation._captured_child_root_id(child),
                            )
                        except Exception as exc:  # noqa: BLE001 - asserted below
                            runner["error"] = exc

                    runner_thread = threading.Thread(target=_run_newer, daemon=True)
                    runner_thread.start()
                    assert newer_entered["RACE-NEWER"].wait(timeout=30), "newer run never reached its gate"
                    assert core.session(child).run_id != expected
                    sweep_release.set()
                    worker.join(timeout=30)
                    assert not worker.is_alive(), "confirm worker never finished"
                    assert "error" not in outcome, outcome.get("error")
                    result = outcome["result"]
                    assert result.status == "ok", result.error
                    assert result.data is not None
                    # The sweep observed the old run; the newer live run is
                    # reported distinctly, never as the old run's cancellation,
                    # and its signal was never touched.
                    assert child in result.data.get("observed_newer_run", ())
                    assert core.session(child).status == "running"
                    newer_release.set()
                    runner_thread.join(timeout=30)
                    assert runner.get("result") is not None and runner["result"].status == "completed", runner.get("error")
                    assert core.store.latest_stop_cause(child, core.session(child).run_id) != "owner_cancel"
            finally:
                sweep_release.set()
                release.set()
                core._delegation.core._cancel_session_run = original_bound  # type: ignore[method-assign]
                worker.join(timeout=30)


def test_stale_confirmation_cannot_cancel_a_real_newer_run(tmp_path: Path) -> None:
    replies = [_turn(output="first answer"), _turn(output="second answer")]
    with _server(replies) as server:
        core = _open(tmp_path, server)
        core.auto_drain = False
        core.create_session("root")
        rev = core.command_catalog().revision_id
        child = core.spawn_child("root", "child work", notify_on_completion=False)
        _wait_for(lambda: core.session(child).status == "completed", what="the first run")
        first_run = core.session(child).run_id
        assert first_run is not None
        pending = core.invoke_command(
            "root", "subagents.monitor.cancel", {"target_session_id": child, "expected_run_id": first_run}, expected_revision=rev
        )
        assert pending.status == "pending_confirmation" and pending.confirmation is not None
        request_id = pending.confirmation.request_id
        receipt = core.send_child("root", child, "real newer run", notify_on_completion=False)
        assert receipt.delivery == "started" and receipt.run_id is not None and receipt.run_id != first_run
        newer_run = core.session(child).run_id
        assert newer_run == receipt.run_id
        # Confirming the stale request must not touch the newer run.
        stale = core.confirm_action("root", request_id, "allow", expected_revision=rev)
        assert stale.status == "error" and "newer run" in (stale.error or "")
        assert core.session(child).run_id == newer_run
        assert core.session(child).status == "running"


def test_reserved_claim_loses_to_first_run_at_boundary(tmp_path: Path) -> None:
    """A bound-null cancel gated at the claim boundary never steals the racing first run."""
    with _server([_turn(output="first answer")]) as server:
        release = threading.Event()
        with _gated_markers(server, ["RESERVED-FIRST"], release=release) as entered:
            core = _open(tmp_path, server)
            core.auto_drain = False
            core.create_session("root")
            rev = core.command_catalog().revision_id
            # Hold the real driver thread before it enters Core._run: the node
            # is genuinely reserved (idle, no run) while we store the request.
            driver_gate = threading.Event()
            driver_release = threading.Event()
            original_run = core._run

            def _gated_run(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
                driver_gate.set()
                assert driver_release.wait(timeout=30), "driver gate never released"
                return original_run(*args, **kwargs)

            core._run = _gated_run  # type: ignore[method-assign]
            try:
                child = core.spawn_child("root", "first run RESERVED-FIRST", notify_on_completion=False)
                assert driver_gate.wait(timeout=30), "driver never reached Core._run gate"
                record = core.session(child)
                assert (record.status, record.run_id) == ("idle", None)
                pending = core.invoke_command(
                    "root", "subagents.monitor.cancel", {"target_session_id": child, "expected_run_id": None}, expected_revision=rev
                )
                assert pending.status == "pending_confirmation" and pending.confirmation is not None
                # Gate the confirm worker at the reserved-claim boundary, then
                # let the driver win the race and block at the provider gate.
                at_claim = threading.Event()
                claim_release = threading.Event()
                original_claim = core._delegation._claim_reserved_child

                def _gated_claim(child_id: str, reason: str) -> bool:
                    if child_id == child and not at_claim.is_set():
                        at_claim.set()
                        assert claim_release.wait(timeout=30), "claim gate never released"
                    return original_claim(child_id, reason)

                core._delegation._claim_reserved_child = _gated_claim  # type: ignore[method-assign]
                outcome: dict[str, object] = {}

                def _confirm() -> None:
                    try:
                        outcome["result"] = core.confirm_action("root", pending.confirmation.request_id, "allow", expected_revision=rev)
                    except Exception as exc:  # noqa: BLE001 - asserted below
                        outcome["error"] = exc

                worker = threading.Thread(target=_confirm, daemon=True)
                try:
                    worker.start()
                    assert at_claim.wait(timeout=30), "sweep never reached the claim boundary"
                    driver_release.set()
                    assert entered["RESERVED-FIRST"].wait(timeout=30), "first run never blocked at provider"
                    claim_release.set()
                    worker.join(timeout=30)
                    assert not worker.is_alive(), "confirm worker never finished"
                    assert "error" not in outcome, outcome.get("error")
                    result = outcome["result"]
                    assert result.status == "ok", result.error
                    assert result.data is not None
                    assert child in result.data.get("observed_newer_run", ())
                    assert child not in result.data.get("claimed_reserved", ())
                    release.set()
                    _wait_for(lambda: core.session(child).status == "completed", what="the first run")
                finally:
                    claim_release.set()
                    driver_release.set()
                    core._delegation._claim_reserved_child = original_claim  # type: ignore[method-assign]
                    worker.join(timeout=30)
            finally:
                driver_release.set()
                core._run = original_run  # type: ignore[method-assign]


def test_parked_settle_pin_refuses_after_newer_run_claims(tmp_path: Path) -> None:
    """A bound parked settle gated at _interrupt_batch never settles the newer run."""
    probe_code = (
        "from voidcode.sdk.plugin import Plugin\n"
        "from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess\n"
        "class Probe:\n"
        "    definition = ToolDefinition(name='park_probe', description='write probe',\n"
        "        input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},\n"
        "        effects=frozenset({ToolEffect.WRITE}))\n"
        "    def invoke(self, call, *, context, services):\n"
        "        return ToolSuccess(call.tool_name, TextOutput('probe ran'))\n"
        "def create_plugin(config):\n"
        "    return Plugin(tools=(Probe(),))\n"
    )
    call = {"id": "probe-1", "type": "function", "function": {"name": "park_probe", "arguments": "{}"}}
    replies = [
        openai_response(message={"role": "assistant", "content": None, "tool_calls": [call]}, finish="tool_calls"),
        _turn(output="denied follow-up done"),
        _turn(output="newer run PARKED-NEWER done"),
    ]
    with _server(replies) as server:
        plugin_dir = tmp_path / "park_plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "__init__.py").write_text(probe_code, encoding="utf-8")
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        config = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": "ask"},
            "providers": {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {
                "files": {"enabled": False},
                "subagents": {"path": str(_SUBAGENTS_ROOT), "enabled": True},
                "park": {"path": str(plugin_dir), "enabled": True},
            },
        }
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        core = open_core(workspace, state_dir=tmp_path / "state")
        _CREATED.append(core)
        core.auto_drain = False
        core.create_session("root")
        rev = core.command_catalog().revision_id
        child = core.spawn_child("root", "park on the probe", notify_on_completion=False)
        _wait_for(lambda: core.session(child).status == "waiting", what="the parked run")
        parked_run = core.session(child).run_id
        assert parked_run is not None
        pending = core.invoke_command(
            "root", "subagents.monitor.cancel", {"target_session_id": child, "expected_run_id": parked_run}, expected_revision=rev
        )
        assert pending.status == "pending_confirmation" and pending.confirmation is not None
        # Install the newer-run gate only now: the parked run's own provider
        # request already completed ungated above.
        release = threading.Event()
        with _gated_markers(server, ["PARKED-NEWER"], release=release) as entered:
            # Gate the confirm worker at the parked-batch boundary: the sweep
            # already passed entry checks with parked_run bound. Then deny the
            # approval, fail the parked run terminally, and start a real newer
            # run blocked at its own provider gate before releasing the old
            # settle. The pin guard (not the entry stale guard) must refuse.
            at_batch = threading.Event()
            batch_release = threading.Event()
            original_interrupt = core._interrupt_batch

            def _gated_interrupt(execution, **kwargs):  # type: ignore[no-untyped-def]
                if not at_batch.is_set():
                    at_batch.set()
                    assert batch_release.wait(timeout=30), "settle gate never released"
                return original_interrupt(execution, **kwargs)

            core._interrupt_batch = _gated_interrupt  # type: ignore[method-assign]
            outcome: dict[str, object] = {}

            def _confirm() -> None:
                try:
                    outcome["result"] = core.confirm_action("root", pending.confirmation.request_id, "allow", expected_revision=rev)
                except Exception as exc:  # noqa: BLE001 - asserted below
                    outcome["error"] = exc

            worker = threading.Thread(target=_confirm, daemon=True)
            try:
                worker.start()
                assert at_batch.wait(timeout=30), "settle never reached the batch boundary"
                approval = next(e for e in core.store.events(child) if e.event_type == "core.approval_requested")
                denied = core.resume(child, approval_request_id=approval.payload["request_id"], decision="deny")
                assert denied.status in ("completed", "failed", "interrupted", "waiting"), denied
                _wait_for(lambda: core.session(child).status in ("completed", "failed"), what="the denied settle")
                runner: dict[str, object] = {}

                def _run_newer() -> None:
                    try:
                        # The sweep holds the subtree fence while gated at the batch
                        # boundary, so a fenced send would be refused; drive the racing
                        # run through the internal run surface with the parent's captured
                        # birth authorization, exactly as the child driver does.
                        runner["result"] = core._run(
                            "real newer run PARKED-NEWER",
                            session_id=child,
                            prompt_max_bytes=CHILD_PROMPT_MAX_BYTES,
                            _root_id=core._delegation._captured_child_root_id(child),
                        )
                    except Exception as exc:  # noqa: BLE001 - asserted below
                        runner["error"] = exc

                runner_thread = threading.Thread(target=_run_newer, daemon=True)
                runner_thread.start()
                assert entered["PARKED-NEWER"].wait(timeout=30), "newer run never blocked at provider"
                assert core.session(child).run_id != parked_run
                batch_release.set()
                worker.join(timeout=30)
                assert not worker.is_alive(), "confirm worker never finished"
                assert "error" not in outcome, outcome.get("error")
                result = outcome["result"]
                assert result.status == "ok", result.error
                assert result.data is not None
                assert child in result.data.get("observed_newer_run", ())
                assert core.session(child).status == "running"
                release.set()
                runner_thread.join(timeout=30)
                assert runner.get("result") is not None and runner["result"].status == "completed", runner.get("error")
                assert core.store.latest_stop_cause(child, core.session(child).run_id) != "owner_cancel"
            finally:
                batch_release.set()
                release.set()
                core._interrupt_batch = original_interrupt  # type: ignore[method-assign]
                worker.join(timeout=30)


def test_late_completion_notice_cannot_restart_a_cancelled_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([_turn(output="done") for _ in range(12)]) as server:
        release = threading.Event()
        late_terminal = threading.Event()
        delivered = threading.Event()
        with _gated_markers(server, ["NOTICE-PARENT", "NOTICE-DESCENDANT"], release=release) as entered:
            core = _open(tmp_path, server)
            core.create_session("root")
            child = core.spawn_child("root", "NOTICE-PARENT", notify_on_completion=False)
            assert entered["NOTICE-PARENT"].wait(timeout=30)
            grandchild = core.spawn_child(child, "NOTICE-DESCENDANT", notify_on_completion=True)
            assert entered["NOTICE-DESCENDANT"].wait(timeout=30)
            child_run = core.session(child).run_id
            grandchild_run = core.session(grandchild).run_id
            original_commit = core._commit
            original_deliver = core._delegation._deliver_child_notice

            def hold_terminal(execution, status, kind, *events, **kwargs):
                if execution.session.session_id == grandchild and status == "interrupted" and kind == "terminal":
                    assert late_terminal.wait(timeout=30), "grandchild terminal gate never released"
                return original_commit(execution, status, kind, *events, **kwargs)

            def observe_delivery(session_id, continuation_id=None):
                try:
                    return original_deliver(session_id, continuation_id)
                finally:
                    if session_id == child:
                        delivered.set()

            monkeypatch.setattr(core, "_commit", hold_terminal)
            monkeypatch.setattr(core._delegation, "_deliver_child_notice", observe_delivery)
            try:
                receipt = core.cancel_descendant_subtree("root", child, expected_run_id=child_run)
                assert set(receipt.cancelled) == {child, grandchild}
                release.set()
                _wait_for(lambda: core.session(child).status == "interrupted", what="parent cancellation before the late notice")
                late_terminal.set()
                assert delivered.wait(timeout=30), "late completion notice was not delivered"
                assert core.session(child).run_id == child_run
                assert core.session(child).status == "interrupted"
                assert core.store.latest_stop_cause(child, child_run) == "owner_cancel"
                notice_id = child_completion_notice_id(child, grandchild, grandchild_run)
                assert any(row.continuation_id == notice_id for row in core.store.pending_continuations(child, limit=100))

                # A later explicit follow-up is new intent, not an automatic revival.
                fresh = core.send_child("root", child, "fresh follow-up", notify_on_completion=False)
                outcome = core.join_child("root", child, timeout_seconds=30)
                assert outcome.status == "completed" and outcome.run_id == fresh.run_id != child_run
            finally:
                late_terminal.set()
                release.set()


@pytest.mark.parametrize("operation", ["spawn", "send"])
def test_cancelled_sdk_invocation_cannot_register_work_after_the_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    registration = openai_response(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "late-register", "type": "function", "function": {"name": "late_register", "arguments": "{}"}}],
        },
        finish="tool_calls",
    )
    replies = [
        _turn(output="parent ready"),
        _turn(output="grandchild ready"),
        registration,
        registration,
        _turn(output="fresh"),
        _turn(output="fresh"),
    ]
    with _server(replies) as server:
        core = _open(tmp_path, server)
        core.create_session("root")
        source = tmp_path / "late_registration"
        source.mkdir()
        (source / "__init__.py").write_text(
            """
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import TextOutput, ToolDefinition, ToolEffect, ToolSuccess

class Register:
    definition = ToolDefinition(
        name="late_register",
        description="Register child work at a cancellation boundary.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
    )
    def invoke(self, call, *, context, services):
        if OPERATION == "spawn":
            services.spawn_child("fresh child", notify_on_completion=False)
        else:
            child = services.list_descendants()[0].session_id
            services.send_child(child, "fresh follow-up", notify_on_completion=False)
        return ToolSuccess(call.tool_name, TextOutput("registered"))

def create_plugin(config):
    return Plugin(tools=(Register(),))
""".replace("OPERATION", repr(operation)),
            encoding="utf-8",
        )
        config_path = core.workspace / "voidcode.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["plugins"]["late_registration"] = {"path": str(source)}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        reloaded = core.invoke_command("root", "core.reload", {}, expected_revision=core.revision_id)
        assert reloaded.status == "ok", reloaded.error
        child = core.spawn_child("root", "prepare parent", notify_on_completion=False)
        assert core.join_child("root", child, timeout_seconds=30).status == "completed"
        grandchild = core.spawn_child(child, "prepare descendant", notify_on_completion=False)
        assert core.join_child(child, grandchild, timeout_seconds=30).status == "completed"
        old_grandchild_run = core.session(grandchild).run_id
        method = "create_child_session" if operation == "spawn" else "enqueue_continuation"
        original = getattr(core.store, method)
        at_registration = threading.Event()
        release_registration = threading.Event()
        outcome: dict[str, object] = {}

        def hold_registration(*args, **kwargs):
            if operation == "send":
                target = kwargs.get("session_id", args[2] if len(args) > 2 else None)
                if target == child:
                    # Pass through the outer parent-authorized send that starts the
                    # old invocation; only the tool's own registration gates here.
                    return original(*args, **kwargs)
            at_registration.set()
            assert release_registration.wait(timeout=30), "SDK registration gate never released"
            return original(*args, **kwargs)

        def run_old_invocation():
            try:
                sent = core.send_child("root", child, "register child work", notify_on_completion=False)
                outcome["receipt"] = sent
                outcome["result"] = core.join_child("root", child, timeout_seconds=30)
            except Exception as exc:
                outcome["error"] = exc

        monkeypatch.setattr(core.store, method, hold_registration)
        runner = threading.Thread(target=run_old_invocation, daemon=True)
        try:
            runner.start()
            assert at_registration.wait(timeout=30), "SDK did not reach its store boundary"
            receipt = core.cancel_descendant_subtree("root", child, expected_run_id=core.session(child).run_id)
            assert child in receipt.cancelled
            # The fence has returned; the original invocation's cancellation
            # must still refuse the already-entered SDK call at the transaction.
            release_registration.set()
            runner.join(timeout=30)
            assert not runner.is_alive()
            assert "error" not in outcome, outcome.get("error")
            # Join vocabulary for an owner-cancelled run: session interrupted with
            # the owner cause surfaces as cancelled, never completed.
            assert outcome["result"].status == "cancelled"
            assert outcome["result"].stop_cause == "owner_cancel"
            assert {node.session_id for node in core.list_descendants("root")} == {child, grandchild}
            assert core.session(grandchild).run_id == old_grandchild_run
            assert core.store.pending_continuations(grandchild, limit=100) == ()

            monkeypatch.setattr(core.store, method, original)
            sent = core.send_child("root", child, "register fresh child work", notify_on_completion=False)
            assert sent.delivery == "started", sent
            fresh = core.join_child("root", child, timeout_seconds=30)
            assert fresh.status == "completed", fresh
            if operation == "spawn":
                new_nodes = [node for node in core.list_descendants(child) if node.session_id != grandchild]
                assert len(new_nodes) == 1 and new_nodes[0].parent_session_id == child
            else:
                _wait_for(lambda: core.session(grandchild).status == "completed", what="fresh SDK follow-up")
                assert core.session(grandchild).run_id != old_grandchild_run
        finally:
            release_registration.set()
            runner.join(timeout=30)


def test_newer_run_keeps_queued_input_at_the_disarm_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([_turn(output="old answer"), _turn(output="new answer")]) as server:
        release = threading.Event()
        with _gated_markers(server, ["DISARM-OLD"], release=release) as entered:
            core = _open(tmp_path, server)
            core.auto_drain = False
            core.create_session("root")
            child = core.spawn_child("root", "DISARM-OLD", notify_on_completion=False)
            assert entered["DISARM-OLD"].wait(timeout=30)
            old_run = core.session(child).run_id
            delivery_release = threading.Event()
            delivery_done = threading.Event()
            original_delivery = core._delegation._deliver_child_messages

            def hold_delivery(*args, **kwargs):
                try:
                    assert delivery_release.wait(timeout=30), "queued delivery gate never released"
                    return original_delivery(*args, **kwargs)
                finally:
                    delivery_done.set()

            monkeypatch.setattr(core._delegation, "_deliver_child_messages", hold_delivery)
            queued = core.send_child("root", child, "INPUT-FOR-NEXT-RUN", notify_on_completion=False)
            assert queued.delivery == "queued"
            release.set()
            _wait_for(lambda: core.session(child).status == "completed", what="old run completion")
            at_disarm = threading.Event()
            release_disarm = threading.Event()
            at_merge = threading.Event()
            release_merge = threading.Event()
            original_disarm = core.store.cancel_pending_continuations
            original_merge = core._delegation._merge_child_messages
            outcome: dict[str, object] = {}

            def hold_disarm(*args, **kwargs):
                at_disarm.set()
                assert release_disarm.wait(timeout=30), "disarm gate never released"
                return original_disarm(*args, **kwargs)

            def hold_merge(execution):
                if execution.session.session_id == child and execution.run_id != old_run:
                    at_merge.set()
                    assert release_merge.wait(timeout=30), "new run merge gate never released"
                return original_merge(execution)

            def cancel_old():
                outcome["cancel"] = core.cancel_descendant_subtree("root", child, expected_run_id=old_run)

            def run_new():
                # The canceller holds the subtree fence while gated at disarm, so a
                # fenced send would be refused; drive the racing run through the
                # internal run surface with the parent's captured birth authorization,
                # exactly as the child driver does.
                outcome["run"] = core._run(
                    "explicit newer turn",
                    session_id=child,
                    prompt_max_bytes=CHILD_PROMPT_MAX_BYTES,
                    _root_id=core._delegation._captured_child_root_id(child),
                )

            monkeypatch.setattr(core.store, "cancel_pending_continuations", hold_disarm)
            monkeypatch.setattr(core._delegation, "_merge_child_messages", hold_merge)
            canceller = threading.Thread(target=cancel_old, daemon=True)
            runner = threading.Thread(target=run_new, daemon=True)
            try:
                canceller.start()
                assert at_disarm.wait(timeout=30)
                runner.start()
                assert at_merge.wait(timeout=30), "newer run never claimed the session"
                release_disarm.set()
                canceller.join(timeout=30)
                assert not canceller.is_alive()
                assert child in outcome["cancel"].observed_newer_run
                assert any(row.payload.get("prompt") == "INPUT-FOR-NEXT-RUN" for row in core.store.pending_continuations(child, limit=100))
                release_merge.set()
                runner.join(timeout=30)
                assert not runner.is_alive() and outcome["run"].status == "completed"
                assert "INPUT-FOR-NEXT-RUN" in _request_texts(server.requests[-1])
            finally:
                release_disarm.set()
                release_merge.set()
                canceller.join(timeout=30)
                if runner.ident is not None:
                    runner.join(timeout=30)
                delivery_release.set()
                assert delivery_done.wait(timeout=30), "queued sender did not finish"


def test_reserved_cancel_wins_without_exposing_a_fake_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server([_turn(output="fresh follow-up")]) as server:
        core = _open(tmp_path, server)
        core.create_session("root")
        at_driver = threading.Event()
        release_driver = threading.Event()
        driver_done = threading.Event()
        original_driver = core._delegation._run_child

        def hold_driver(*args, **kwargs):
            at_driver.set()
            try:
                assert release_driver.wait(timeout=30), "initial driver gate never released"
                return original_driver(*args, **kwargs)
            finally:
                driver_done.set()

        monkeypatch.setattr(core._delegation, "_run_child", hold_driver)
        child = core.spawn_child("root", "original unstarted task", notify_on_completion=False)
        try:
            assert at_driver.wait(timeout=30)
            receipt = core.cancel_descendant_subtree("root", child, expected_run_id=None)
            assert receipt.claimed_reserved == (child,)
            node = next(node for node in core.list_descendants("root") if node.session_id == child)
            assert node.run_id is None and node.status == "interrupted" and node.stop_cause == "owner_cancel"
            again = core.cancel_descendant_subtree("root", child, expected_run_id=None)
            assert again.already_terminal == (child,)
            release_driver.set()
            assert driver_done.wait(timeout=30)
            assert server.requests == [], "the cancelled initial driver called the provider"

            fresh = core.send_child("root", child, "explicit fresh task", notify_on_completion=False)
            outcome = core.join_child("root", child, timeout_seconds=30)
            assert outcome.status == "completed" and outcome.run_id == fresh.run_id
            assert outcome.output == "fresh follow-up"
        finally:
            release_driver.set()
            assert driver_done.wait(timeout=30)
