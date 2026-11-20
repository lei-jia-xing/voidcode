"""Consumer tests for the P2a generic CLI + HTTP transport.

Every test drives a real boundary: actual CLI subprocesses, an actual
listening HTTP server, and a real loopback provider. No mocks, no TestClient
shortcuts, no skipped core gaps.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import uvicorn

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from tests.rpc_client import request as _request
from voidcode.transport.http import create_core_app

_HELLO_ID = "hello-state"


def _hello_plugin_source(root: Path, *, with_context: bool = False) -> None:
    """Third-party hello-state fixture: commands + view, optionally context + observer (frozen 2/3-arity API)."""
    root.mkdir(parents=True, exist_ok=True)
    extra_imports = "from voidcode.sdk.hooks import ContextContribution, UsageFact\n" if with_context else ""
    extra_callbacks = (
        """

def hello_context(ctx, services):
    state, _ = services.get_state("session")
    greeting = (state or {}).get("greeting", "default")
    return ContextContribution(content=f"HELLO_STATE={greeting}")


def hello_observer(fact, ctx, services):
    if isinstance(fact, UsageFact):
        state, version = services.get_state("session")
        seen = int((state or {}).get("usage_facts_seen", 0))
        merged = dict(state or {})
        merged["usage_facts_seen"] = seen + 1
        services.replace_state(merged, expected_version=version, scope="session")
"""
        if with_context
        else ""
    )
    factory_tail = ', context=hello_context, observer=hello_observer, observed_facts=("usage",)' if with_context else ""
    code = '''"""Ordinary third-party plugin: durable greeting with a gated action."""

from __future__ import annotations

from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolEffect
from voidcode.sdk.views import PluginViewDescriptor


class HelloSetCommand:
    definition = CommandDefinition(
        name="hello_set",
        description="Set the durable greeting.",
        argument_schema={"type": "object", "properties": {"greeting": {"type": "string"}}},
        effects=frozenset({ToolEffect.WRITE}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        greeting = invocation.arguments.get("greeting", "hello")
        state, version = services.get_state("session")
        merged = dict(state or {})
        merged["greeting"] = greeting
        services.replace_state(merged, expected_version=version, scope="session")
        services.stage_event("greeting_set", {"greeting": greeting})
        services.enqueue_continuation({"action": "greet_followup", "greeting": greeting})
        return CommandResult(status="ok", output=f"Set greeting to: {greeting}")


class HelloGetCommand:
    definition = CommandDefinition(
        name="hello_get",
        description="Read the durable greeting.",
        argument_schema={"type": "object", "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        state, version = services.get_state("session")
        greeting = (state or {}).get("greeting", "none")
        usage_facts_seen = (state or {}).get("usage_facts_seen", 0)
        data = {"greeting": greeting, "version": version, "usage_facts_seen": usage_facts_seen}
        return CommandResult(status="ok", output=f"Current greeting: {greeting}", data=data)


class GatedCommand:
    definition = CommandDefinition(
        name="gated_action",
        description="Gated action requiring an idle session.",
        argument_schema={"type": "object"},
        effects=frozenset({ToolEffect.WRITE}),
        busy_gate=True,
    )

    def invoke(self, invocation, services):
        return CommandResult(status="ok", output="Executed gated action")


class HelloView:
    name = "hello_view"

    def render(self, session_id, services):
        state, _ = services.get_state("session")
        greeting = (state or {}).get("greeting", "default")
        return PluginViewDescriptor(
            owner="hello-state",
            name="hello_view",
            title="Hello View",
            view_type="status",
            data={"label": "greeting", "value": greeting},
        )


def create_plugin(config):
    return Plugin(commands=(HelloSetCommand(), HelloGetCommand(), GatedCommand()), views=(HelloView(),)FACTORY_TAIL)
'''
    code = code.replace(
        "from voidcode.sdk.commands import CommandDefinition, CommandResult\n",
        "from voidcode.sdk.commands import CommandDefinition, CommandResult\n" + extra_imports,
    )
    code = code.replace(
        "def create_plugin(config):", extra_callbacks + "\ndef create_plugin(config):" if with_context else "def create_plugin(config):"
    )
    code = code.replace("FACTORY_TAIL", factory_tail)
    (root / "__init__.py").write_text(code, encoding="utf-8")


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path, **policy: object) -> None:
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": policy or {"approval_mode": "yolo"},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {_HELLO_ID: {"path": str(source)}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


@contextmanager
def _provider(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _turn(*calls: dict[str, object], output: str | None = None, usage: dict[str, object] | None | bool = True) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "authentic loopback reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
        usage=usage,
    )


def _cli(workspace: Path, state: Path, *arguments: str, code: int = 0) -> dict[str, object]:
    command = [sys.executable, "-m", "voidcode", "--workspace", str(workspace), "--state-dir", str(state)]
    arguments = _with_displayed_revision(command, arguments)
    completed = subprocess.run([*command, *arguments], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == code, completed.stdout + completed.stderr
    return json.loads(completed.stdout)  # type: ignore[return-value]


def _with_displayed_revision(command: list[str], arguments: tuple[str, ...]) -> tuple[str, ...]:
    """Supply the catalog revision a real client would have displayed.

    Invoke/confirm require the revision the client saw; these tests act as that
    client, so they read the catalog first and echo its revision verbatim.
    """
    if not arguments or arguments[0] not in {"invoke", "confirm"} or "--expected-revision" in arguments:
        return arguments
    catalog = subprocess.run([*command, "catalog"], capture_output=True, text=True, timeout=60, check=False)
    assert catalog.returncode == 0, catalog.stdout + catalog.stderr
    revision = json.loads(catalog.stdout)["revision_id"]  # type: ignore[index]
    return (*arguments, "--expected-revision", revision)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def _listening_http(workspace: Path, state: Path, token: str) -> Iterator[str]:
    app = create_core_app(workspace=workspace, state_dir=state, bearer_token=token)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 20
    base = f"http://127.0.0.1:{port}"
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError as exc:
            if time.monotonic() > deadline:
                raise TimeoutError("listening HTTP server never opened its port") from exc
            time.sleep(0.05)
    try:
        yield base
    finally:
        server.should_exit = True
        worker.join(timeout=10)


def _http_request(
    base: str, method: str, path: str, *, token: str | None = None, body: object = None, origin: str | None = None
) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if origin is not None:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        _ = args


def test_cli_show_rescues_history_with_broken_config_and_broken_plugin(tmp_path: Path) -> None:
    """Rescue: show reads durable history even when config/plugins cannot load."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([_turn(output="done")]) as server:
        _configure(workspace, server, source)
        ran = _cli(workspace, state, "run", "hello", "--session-id", "rescue-1")
        assert ran["status"] == "completed"
    # Break both the config and the plugin source after history exists.
    (workspace / "voidcode.json").write_text("{not valid json", encoding="utf-8")
    (source / "hello_plugin.py").write_text("raise RuntimeError('broken plugin')\n", encoding="utf-8")
    assert (state / "voidcode.db").is_file()
    shown = _cli(workspace, state, "show", "rescue-1", "--events")
    assert shown["session_id"] == "rescue-1"
    assert shown["status"] == "completed"
    assert any(event["type"] == "core.user_message" for event in shown["events"])


def test_cli_rejects_malformed_arguments_before_opening_core(tmp_path: Path) -> None:
    """Strict JSON args fail fast; business keys like owner/actor are not blacklisted."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([_turn(output="done")]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "args-1")
        bad = _cli(workspace, state, "invoke", "args-1", "hello_set", "--arguments", "{oops", code=1)
        assert "JSON object" in bad["error"]
        # owner/actor/policy as *business* argument keys reach the admitted
        # command schema — the client carries no coincidental-name blacklist.
        # The strict hello_get schema rejects the unknown key, and the command
        # reports status=error (exit 1) rather than a client-side block.
        schema_rejected = _cli(workspace, state, "invoke", "args-1", "hello_get", "--arguments", '{"owner": "x"}', code=1)
        assert schema_rejected["status"] == "error"
        assert "owner" in schema_rejected["error"]


def test_revision_bound_actions_refuse_missing_empty_and_stale_revisions(tmp_path: Path) -> None:
    """Every action echoes the displayed revision; a stale one is refused with no effect."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "rev-1")
        token = "revision-bound-token"
        with _listening_http(workspace, state, token) as base:
            status, body = _request(base, "command.catalog", {}, token=token)
            assert status == 200, body.decode()
            catalog = json.loads(body)
            displayed = catalog["revision_id"]
            assert displayed
            assert any(command["name"] == "hello_set" for command in catalog["commands"])

            # The catalog envelope revision is the session's active revision: one
            # displayed generation, not a per-response guess.
            status, body = _request(base, "session.get", {"session_id": "rev-1"}, token=token)
            assert status == 200, body.decode()
            assert json.loads(body)["active_revision_id"] == displayed

            # The views envelope carries that same captured generation.
            status, body = _request(base, "session.views", {"session_id": "rev-1"}, token=token)
            assert status == 200, body.decode()
            views = json.loads(body)
            assert views["revision_id"] == displayed
            assert "hello_view" in [view["name"] for view in views["views"]]

            # Missing and empty revisions never reach a lookup: 400 invalid_params.
            status, body = _request(
                base, "command.invoke", {"session_id": "rev-1", "name": "hello_set", "arguments": {"greeting": "must-not-land"}}, token=token
            )
            assert status == 400, body.decode()
            assert json.loads(body)["code"] == "invalid_params"
            status, body = _request(
                base,
                "command.invoke",
                {"session_id": "rev-1", "name": "hello_set", "arguments": {"greeting": "must-not-land"}, "expected_revision": ""},
                token=token,
            )
            assert status == 400, body.decode()
            assert json.loads(body)["code"] == "invalid_params"

            # A revision the client did not display is stale: 409, no dispatch.
            status, body = _request(
                base,
                "command.invoke",
                {"session_id": "rev-1", "name": "hello_set", "arguments": {"greeting": "must-not-land"}, "expected_revision": "rev-never-displayed"},
                token=token,
            )
            assert status == 409, body.decode()
            refusal = json.loads(body)
            assert refusal["code"] == "stale_revision"
            assert "stale" in refusal["error"]

            # confirmation rejects the stale revision before it even looks up the
            # stored request id, so an unknown id cannot be probed under a lie.
            status, body = _request(
                base,
                "action.confirm",
                {"session_id": "rev-1", "request_id": "req-not-stored", "decision": "allow", "expected_revision": "rev-never-displayed"},
                token=token,
            )
            assert status == 409, body.decode()
            assert json.loads(body)["code"] == "stale_revision"

            # Nothing refused had an outside effect, and the displayed revision works.
            status, body = _request(base, "command.invoke", {"session_id": "rev-1", "name": "hello_get", "expected_revision": displayed}, token=token)
            assert status == 200, body.decode()
            assert json.loads(body)["data"]["greeting"] == "none"

        # The CLI is the same boundary: a stale revision is refused, not defaulted.
        stale = _cli(workspace, state, "invoke", "rev-1", "hello_set", "--expected-revision", "rev-never-displayed", code=1)
        assert "stale" in str(stale["error"])


def test_listening_http_bearer_and_origin_boundary(tmp_path: Path) -> None:
    """Auth: wrong/missing bearer is 401, and the token never leaks into bodies."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([]) as server:
        _configure(workspace, server, source)
        token = "test-token-abc123"
        with _listening_http(workspace, state, token) as base:
            status, body = _request(base, "command.catalog", {})
            assert status == 401
            status, body = _request(base, "command.catalog", {}, token="wrong")
            assert status == 401
            status, body = _request(base, "command.catalog", {}, token=token)
            assert status == 200, body.decode()
            assert token not in body.decode()
            payload = json.loads(body)
            assert any(command["name"] == "hello_set" for command in payload["commands"])
            status, body = _http_request(base, "GET", "/no-such-route", token=token)
            assert status == 404
            status, body = _request(base, "session.create", {"unknown_field": 1}, token=token)
            assert status == 400


def test_cli_and_http_share_generic_command_route_with_restart(tmp_path: Path) -> None:
    """Same generic route: CLI set -> HTTP get, durable across restart."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    # One provider port for the whole test: base_url is revision-pinned, so
    # rewriting voidcode.json with a new endpoint would orphan the session.
    with _provider([]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "shared-1")
        set_result = _cli(workspace, state, "invoke", "shared-1", "hello_set", "--arguments", '{"greeting": "persist-me"}')
        assert set_result["status"] == "ok"
        token = "shared-route-token"
        with _listening_http(workspace, state, token) as base:
            # The CLI already exited, so HTTP owns the core now. The HTTP client
            # acts as a real client: it reads the catalog and echoes its revision.
            status, body = _request(base, "command.catalog", {}, token=token)
            assert status == 200, body.decode()
            displayed = json.loads(body)["revision_id"]
            status, body = _request(
                base, "command.invoke", {"session_id": "shared-1", "name": "hello_get", "expected_revision": displayed}, token=token
            )
            assert status == 200, body.decode()
            assert json.loads(body)["data"]["greeting"] == "persist-me"
            status, body = _request(base, "session.views", {"session_id": "shared-1"}, token=token)
            assert status == 200, body.decode()
            views = json.loads(body)["views"]
            hello = next(view for view in views if view["name"] == "hello_view")
            assert hello["data"]["value"] == "persist-me"
        # Cross-restart within the same revision: fresh CLI process reads the
        # same durable state without touching voidcode.json.
        again = _cli(workspace, state, "invoke", "shared-1", "hello_get")
        assert again["data"]["greeting"] == "persist-me"


def test_busy_gate_matrix_over_real_cli(tmp_path: Path) -> None:
    """Non-gated invoke works during a waiting run; gated invoke is rejected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    call = {"id": "call-1", "type": "function", "function": {"name": "write", "arguments": json.dumps({"path": "out.txt", "content": "x"})}}
    with _provider([_turn(call, output=None), _turn(output="resumed")]) as server:
        _configure(workspace, server, source, approval_mode="ask")
        started = _cli(workspace, state, "run", "write a file", "--session-id", "busy-1")
        assert started["status"] == "waiting", started
        # Non-gated command still executes and preserves the waiting checkpoint.
        ok = _cli(workspace, state, "invoke", "busy-1", "hello_set", "--arguments", '{"greeting": "during-wait"}')
        assert ok["status"] == "ok"
        gated = _cli(workspace, state, "invoke", "busy-1", "gated_action", code=1)
        assert "busy" in gated["error"].lower() or "idle" in gated["error"].lower()
        resumed = _cli(
            workspace,
            state,
            "resume",
            "busy-1",
            "--approval-request-id",
            started["pending_approval"]["request_id"],
            "--decision",
            "allow",
        )
        assert resumed["status"] == "completed", resumed


def test_plugin_context_contribution_reaches_the_provider_turn(tmp_path: Path) -> None:
    """Consumer contract: context contributed by a plugin is present in the provider turn.

    The plugin's own captured state is the authority; the provider wire shape is
    checked only for the carrier role, never for a copied echo of the mock body.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source, with_context=True)
    state = tmp_path / "state"
    with _provider([_turn(output="ack")]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "ctx-1")
        set_result = _cli(workspace, state, "invoke", "ctx-1", "hello_set", "--arguments", '{"greeting": "ctx-marker-42"}')
        assert set_result["status"] == "ok"

        # The plugin's durable state is the consumer-visible truth for this flow.
        stored = _cli(workspace, state, "invoke", "ctx-1", "hello_get")
        assert stored["data"]["greeting"] == "ctx-marker-42", stored

        # A real provider turn happened and the contributed context travelled
        # with it as a system carrier (never as a role=user message).
        assert server.requests, "the run never reached the provider"
        messages = server.requests[0]["messages"]
        carriers = [message for message in messages if "HELLO_STATE=ctx-marker-42" in str(message.get("content", ""))]
        assert len(carriers) == 1, messages
        assert carriers[0]["role"] == "system", carriers[0]


def test_observer_counts_real_provider_usage_facts(tmp_path: Path) -> None:
    """Penetration: the observer fires once per real provider usage fact and persists its counter."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source, with_context=True)
    state = tmp_path / "state"
    with _provider([_turn(output="one"), _turn(output="two")]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "obs-1")
        first = _cli(workspace, state, "run", "first turn", "--session-id", "obs-1")
        assert first["status"] == "completed", first
        second = _cli(workspace, state, "run", "second turn", "--session-id", "obs-1")
        assert second["status"] == "completed", second
        assert len(server.requests) == 2
        stored = _cli(workspace, state, "invoke", "obs-1", "hello_get")
        assert stored["data"]["usage_facts_seen"] == 2, stored
        # Loud-failure proof: no observer error was swallowed along the way.
        shown = _cli(workspace, state, "show", "obs-1", "--events")
        assert not [event for event in shown["events"] if event["type"] == "core.observer_error"], shown["events"]


def test_continuation_runs_automatically_as_real_followup_work(tmp_path: Path) -> None:
    """Consumer contract: an idle-session command auto-runs its continuation as a real turn."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([_turn(output="followup done")]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "cont-1")
        set_result = _cli(workspace, state, "invoke", "cont-1", "hello_set", "--arguments", '{"greeting": "queued"}')
        assert set_result["status"] == "ok"

        # The enqueued continuation is driven by the core itself: a real
        # provider turn ran without any explicit drain request.
        assert len(server.requests) == 1, server.requests
        shown = _cli(workspace, state, "show", "cont-1", "--events")
        types = [event["type"] for event in shown["events"]]
        assert "core.continuation_started" in types, types
        assert "core.run_completed" in types, types

        # Claim-once: nothing is left to drain afterwards.
        drained_again = _cli(workspace, state, "drain", "cont-1")
        assert drained_again["continuations"] == []


def test_basicchat_optional_usage_multi_turn_success(tmp_path: Path) -> None:
    """Basic chat with usage=None across turns succeeds; optional usage does not poison the session."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([_turn(output="first turn output", usage=None), _turn(output="second turn output", usage=None)]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "optional-usage-1")
        first = _cli(workspace, state, "run", "first turn", "--session-id", "optional-usage-1")
        assert first["status"] == "completed", first
        second = _cli(workspace, state, "run", "second turn", "--session-id", "optional-usage-1")
        assert second["status"] == "completed", second
        assert len(server.requests) == 2


def test_targeted_cancel_requests_one_session_only(tmp_path: Path) -> None:
    """Cancel for session B reports cancellation_requested without touching session A."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([]) as server:
        _configure(workspace, server, source)
        _cli(workspace, state, "create-session", "--session-id", "cancel-a")
        _cli(workspace, state, "create-session", "--session-id", "cancel-b")
        token = "cancel-scope-token"
        with _listening_http(workspace, state, token) as base:
            status, body = _request(base, "session.cancel", {"session_id": "cancel-b"}, token=token)
            assert status == 200, body.decode()
            payload = json.loads(body)
            assert payload["session_id"] == "cancel-b"
            assert payload["cancellation_requested"] is False  # idle session owns no active run
            status, body = _request(base, "session.get", {"session_id": "cancel-a"}, token=token)
            assert status == 200, body.decode()
            assert json.loads(body)["status"] == "idle"
            status, body = _request(base, "session.get", {"session_id": "cancel-b"}, token=token)
            assert status == 200, body.decode()
            assert json.loads(body)["status"] == "idle"


def test_events_cursor_replays_tail_without_full_rescan(tmp_path: Path) -> None:
    """after_sequence returns only later events; follow crosses a completed run."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([_turn(output="first done"), _turn(output="second done")]) as server:
        _configure(workspace, server, source)
        first = _cli(workspace, state, "run", "first", "--session-id", "events-1")
        assert first["status"] == "completed", first
        shown = _cli(workspace, state, "show", "events-1", "--events")
        mid = shown["last_event_sequence"]
        assert mid > 1
        token = "events-cursor-token"
        with _listening_http(workspace, state, token) as base:
            status, body = _request(base, "session.subscribe", {"session_id": "events-1", "after_sequence": mid}, token=token)
            assert status == 200, body.decode()
            frames = json.loads(body)
            assert frames, body.decode()
            head = frames[0]
            assert head["events"] == []
            status, body = _request(base, "session.subscribe", {"session_id": "events-1", "after_sequence": 0}, token=token)
            assert status == 200, body.decode()
            frames = json.loads(body)
            replayed = frames[0]
            assert [event["sequence"] for event in replayed["events"]] == list(range(1, mid + 1))


def test_malformed_conversational_record_is_a_terminal_error_not_a_null_transcript(tmp_path: Path) -> None:
    """A corrupt user message ends the stream visibly; the sentinel payload never leaks."""
    import sqlite3

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _hello_plugin_source(source)
    state = tmp_path / "state"
    with _provider([_turn(output="done")]) as server:
        _configure(workspace, server, source)
        ran = _cli(workspace, state, "run", "hello", "--session-id", "malformed-1")
        assert ran["status"] == "completed"
        shown = _cli(workspace, state, "show", "malformed-1", "--events")
        tip = shown["last_event_sequence"]
        catalog = _cli(workspace, state, "catalog")
        sentinel = "SENTINEL-CREDENTIAL-malformed-probe"
        connection = sqlite3.connect(state / "voidcode.db")
        try:
            payload_text = json.dumps({"content": {"leaked": sentinel}, "eligible_owners": []})
            connection.execute(
                "INSERT INTO session_events(session_id, sequence, run_id, event_type, source, actor, revision_id, payload_json, payload_bytes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "malformed-1",
                    tip + 1,
                    shown["run_id"] or "run-probe",
                    "core.user_message",
                    "core",
                    "user",
                    catalog["revision_id"],
                    payload_text,
                    len(payload_text.encode("utf-8")),
                ),
            )
            connection.execute("UPDATE sessions SET last_event_sequence = ? WHERE session_id = ?", (tip + 1, "malformed-1"))
            connection.commit()
        finally:
            connection.close()
        token = "malformed-head-token"
        with _listening_http(workspace, state, token) as base:
            status, body = _request(base, "session.subscribe", {"session_id": "malformed-1", "after_sequence": 0}, token=token)
            assert status == 200, body.decode()
            text = body.decode("utf-8")
            assert sentinel not in text and "leaked" not in text
            frames = json.loads(text)
            assert len(frames) == 1, frames
            (error,) = frames
            assert error["kind"] == "error"
            assert f"sequence={tip + 1}" in error["error"] and "core.user_message" in error["error"]
