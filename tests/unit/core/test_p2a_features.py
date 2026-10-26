"""End-to-end behavior tests for P2a Core & SDK features:
- Plugin state management, context contribution, and fact observation
- Action confirmation lifecycle with state and version inspection
- Governed durable continuation execution with real provider turns and recovery
- Windowed usage reporting with dispatch sequence attribution and unknown usage isolation
- Strict rejection of reserved core command names and prefixes
"""

import uuid
from pathlib import Path

import pytest

from voidcode.core.activation import ExecutionConfig, ExecutionPolicy
from voidcode.core.engine import Core
from voidcode.core.plugins import PluginSource
from voidcode.core.storage.store import EventDraft


def make_p2a_plugin(
    tmp_path: Path,
    *,
    plugin_id: str = "hello-state",
    command_names: tuple[str, ...] = ("hello_set", "hello_get", "gated_action", "confirm_action"),
    view_names: tuple[str, ...] = ("hello_view",),
    include_context: bool = False,
    include_observer: bool = False,
    observer_raises: bool = False,
    include_provider: bool = False,
    reserved_command: str | None = None,
    extra_command: bool = False,
) -> Path:
    root = tmp_path / plugin_id
    root.mkdir(parents=True, exist_ok=True)

    declared_cmds = list(command_names)
    if extra_command:
        declared_cmds.append("shell_like")
    if reserved_command is not None:
        declared_cmds.append(reserved_command)

    code = f"""
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.commands import CommandDefinition, CommandResult, ConfirmationRequest
from voidcode.sdk.views import PluginViewDescriptor
from voidcode.sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolSuccess
from voidcode.sdk.hooks import ContextContribution
from voidcode.sdk.provider import ProviderTurnResult, ProviderTokenUsage

class HelloSetCommand:
    definition = CommandDefinition(
        name="hello_set",
        description="Set durable greeting",
        argument_schema={{"type": "object", "properties": {{"greeting": {{"type": "string"}}}}}},
        effects=frozenset({{ToolEffect.WRITE}}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        greeting = invocation.arguments.get("greeting", "hello")
        _, ver = services.get_state("session")
        services.replace_state({{"greeting": greeting}}, expected_version=ver, scope="session")
        services.stage_event("greeting_set", {{"greeting": greeting}})
        services.enqueue_continuation({{"action": "greet_followup", "greeting": greeting}})
        return CommandResult(status="ok", output=f"Set greeting to: {{greeting}}")

class HelloGetCommand:
    definition = CommandDefinition(
        name="hello_get",
        description="Get durable greeting",
        argument_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.READ}}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        st, ver = services.get_state("session")
        greeting = (st or {{}}).get("greeting", "none")
        action_done = (st or {{}}).get("action_done", False)
        last_obs = (st or {{}}).get("last_observed", "none")
        return CommandResult(
            status="ok",
            output=f"Greeting: {{greeting}}, Action: {{action_done}}",
            data={{
                "greeting": greeting,
                "action_done": action_done,
                "last_observed": last_obs,
                "version": ver,
                "schema_version": services.get_state_schema_version("session"),
                "observed_owner": (st or {{}}).get("last_owner", ""),
                "observed_effects": (st or {{}}).get("last_effects", []),
            }},
        )

class GatedCommand:
    definition = CommandDefinition(
        name="gated_action",
        description="Gated action that requires idle session",
        argument_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.WRITE}}),
        busy_gate=True,
    )

    def invoke(self, invocation, services):
        return CommandResult(status="ok", output="Executed gated action")

class ConfirmCommand:
    definition = CommandDefinition(
        name="confirm_action",
        description="Action that requests confirmation first",
        argument_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.WRITE}}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        if invocation.context.actor == "user":
            st, ver = services.get_state("session")
            cur = dict(st or {{}})
            cur["action_done"] = True
            services.replace_state(cur, expected_version=ver, scope="session")
            return CommandResult(status="ok", output="Action performed after confirmation")
        _, ver = services.get_state("session")
        req_id = f"req-confirm-{{invocation.context.session_id}}"
        req = ConfirmationRequest(
            request_id=req_id,
            owner="{plugin_id}",
            command_name="confirm_action",
            arguments={{}},
            ui_type="confirmation",
            ui_data={{"request_id": req_id, "message": "Confirm action?", "actions": ["allow", "deny"]}},
            expected_state_version=ver,
        )
        return CommandResult(status="pending_confirmation", confirmation=req)

class ShellLikeCommand:
    definition = CommandDefinition(
        name="shell_like",
        description="Extra command with declarative syntax",
        argument_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.WRITE}}),
        busy_gate=False,
        client_syntax="!",
    )

    def invoke(self, invocation, services):
        st, ver = services.get_state("session")
        cur = dict(st or {{}})
        cur["shell_actor"] = invocation.context.actor
        services.replace_state(cur, expected_version=ver, scope="session")
        return CommandResult(status="ok", output="extra command ran")


class RogueCommand:
    def __init__(self, name):
        self.definition = CommandDefinition(
            name=name,
            description="Reserved command violation",
            argument_schema={{"type": "object"}},
        )

    def invoke(self, invocation, services):
        return CommandResult(status="ok", output="rogue ran")


class HelloView:
    name = "hello_view"

    def render(self, session_id, services):
        st, ver = services.get_state("session")
        greeting = (st or {{}}).get("greeting", "default")
        return PluginViewDescriptor(
            owner="{plugin_id}",
            name="hello_view",
            title="Hello View",
            view_type="status",
            data={{"label": "greeting", "value": greeting}},
        )

class WriteTool:
    definition = ToolDefinition(
        name="write_tool",
        description="Mutating tool requiring approval",
        input_schema={{"type": "object"}},
        effects=frozenset({{ToolEffect.WRITE}}),
    )

    def invoke(self, call, *, context, services):
        return ToolSuccess(tool_name="write_tool", output=TextOutput(text="wrote data successfully"))

class LoopbackProvider:
    name = "endpoint"

    def propose_turn(self, request):
        # Inspect messages for continuation or context
        continuation_msg = next((m for m in request.messages if "durable continuation" in (m.content or "").lower()), None)
        if continuation_msg:
            usage = ProviderTokenUsage(input_tokens=200, output_tokens=100, cache_read_tokens=40, uncached_input_tokens=160)
            return ProviderTurnResult(output="continuation executed successfully", usage=usage, done_reason="stop", finish_reason_reported=True)

        # Check if need_approval requested
        need_app = any("need_approval" in (m.content or "") for m in request.messages)
        has_tool_res = any(m.role == "tool" for m in request.messages)
        if need_app and not has_tool_res:
            usage = ProviderTokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=20, uncached_input_tokens=80)
            return ProviderTurnResult(
                output=None,
                tool_calls=(ToolCall(tool_call_id="call-write-1", tool_name="write_tool", arguments={{}}),),
                usage=usage,
                done_reason="tool_calls",
                finish_reason_reported=True,
            )
        if need_app and has_tool_res:
            usage = ProviderTokenUsage(input_tokens=120, output_tokens=60, cache_read_tokens=30, uncached_input_tokens=90)
            return ProviderTurnResult(output="done after approval", usage=usage, done_reason="stop", finish_reason_reported=True)

        ctx_msg = next((m for m in request.messages if "[hello-state context]" in (m.content or "")), None)
        out_msg = "turn reply with context" if ctx_msg else "turn reply"
        usage = ProviderTokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=20, uncached_input_tokens=80)
        return ProviderTurnResult(output=out_msg, usage=usage, done_reason="stop", finish_reason_reported=True)

def plugin_context(context, services):
    st, _ = services.get_state("session")
    greeting = (st or {{}}).get("greeting", "default")
    return ContextContribution(content=f"greeting active: {{greeting}}")

def plugin_observer(fact, context, services):
    if {observer_raises}:
        raise RuntimeError("observer exploded")
    st, ver = services.get_state("session")
    cur = dict(st or {{}})
    cur["last_observed"] = type(fact).__name__
    if hasattr(fact, "tool_name"):
        # Invocation facts carry the owner and the declared effects that governed the call.
        cur["last_owner"] = getattr(fact, "owner", "")
        cur["last_effects"] = list(getattr(fact, "effects", ()) or ())
    services.replace_state(cur, expected_version=ver, scope="session")

def create_plugin(config):
    commands = []
    if "hello_set" in {declared_cmds!r}:
        commands.append(HelloSetCommand())
    if "hello_get" in {declared_cmds!r}:
        commands.append(HelloGetCommand())
    if "gated_action" in {declared_cmds!r}:
        commands.append(GatedCommand())
    if "confirm_action" in {declared_cmds!r}:
        commands.append(ConfirmCommand())
    if "shell_like" in {declared_cmds!r}:
        commands.append(ShellLikeCommand())
    if {reserved_command!r} in {declared_cmds!r}:
        commands.append(RogueCommand({reserved_command!r}))
    views = []
    if "hello_view" in {list(view_names)!r}:
        views.append(HelloView())

    providers = []
    if {include_provider}:
        providers.append(LoopbackProvider())

    ctx_cb = plugin_context if {include_context} else None
    obs_cb = plugin_observer if {include_observer} else None

    return Plugin(
        commands=tuple(commands),
        views=tuple(views),
        providers=tuple(providers),
        tools=(WriteTool(),) if {include_provider} else (),
        context=ctx_cb,
        observer=obs_cb,
        observed_facts=("usage", "invocation_finished") if {include_observer} else (),
    )
"""
    (root / "__init__.py").write_text(code, encoding="utf-8")
    return root


def make_configured_core(
    tmp_path: Path,
    *,
    include_context: bool = False,
    include_observer: bool = False,
    observer_raises: bool = False,
    include_provider: bool = False,
    extra_command: bool = False,
    read_only: bool = False,
) -> tuple[Core, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"

    root = make_p2a_plugin(
        tmp_path,
        include_context=include_context,
        include_observer=include_observer,
        observer_raises=observer_raises,
        include_provider=include_provider,
        extra_command=extra_command,
    )
    plugin_source = PluginSource(root=root, plugin_id="hello-state", config={})

    config = ExecutionConfig(
        model="endpoint/test-model" if include_provider else None,
        policy=ExecutionPolicy(approval_mode="ask", read_only=read_only),
    )
    core = Core(
        workspace=workspace,
        state_dir=state_dir,
        config=config,
        plugins=(plugin_source,),
        revision_id=uuid.uuid4().hex,
        revision_payload={"revision": "p2a-test"},
        auto_drain=False,
    )
    return core, "hello-state"


def test_p2a_hello_state_with_context_and_persistence(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path, include_context=True, include_observer=True, include_provider=True)
    with core:
        session = core.create_session("sess-p2a-main")
        assert session.session_id == "sess-p2a-main"
        assert session.status == "idle"

        # 1. Verify initial state is none
        get_res0 = core.invoke_command("sess-p2a-main", "hello_get", {}, expected_revision=core.command_catalog().revision_id)
        assert get_res0.status == "ok"
        assert get_res0.data is not None
        assert get_res0.data["greeting"] == "none"
        assert get_res0.data["version"] == 0
        # No row yet: the accessor reports the absence, not a fabricated version.
        assert get_res0.data["schema_version"] == 0

        # 2. Invoke hello_set command
        set_res = core.invoke_command("sess-p2a-main", "hello_set", {"greeting": "awesome"}, expected_revision=core.command_catalog().revision_id)
        assert set_res.status == "ok"

        # 3. Read back updated state through public read command
        get_res1 = core.invoke_command("sess-p2a-main", "hello_get", {}, expected_revision=core.command_catalog().revision_id)
        assert get_res1.status == "ok"
        assert get_res1.data is not None
        assert get_res1.data["greeting"] == "awesome"
        assert get_res1.data["version"] == 1
        # The stored row carries this plugin's declared state schema version.
        assert get_res1.data["schema_version"] == 1

        # 4. Plugin views render from durable state
        views = core.plugin_views("sess-p2a-main").views
        assert len(views) == 1
        assert views[0].name == "hello_view"
        assert views[0].view_type == "status"
        assert views[0].data["value"] == "awesome"

        # 5. Run an agent turn and observe that contributed context is included
        run_res = core.run("say hello", session_id="sess-p2a-main")
        assert run_res.status == "completed"
        assert run_res.output == "turn reply with context"

        # Observer updated durable state through its own lease
        get_res2 = core.invoke_command("sess-p2a-main", "hello_get", {}, expected_revision=core.command_catalog().revision_id)
        assert get_res2.status == "ok"
        assert get_res2.data is not None
        assert get_res2.data["last_observed"] == "UsageFact"

        # Check stored events include user_message, model_turn, and command completion
        events = core.events_after("sess-p2a-main", after_sequence=0)
        types = [e.event_type for e in events]
        assert "core.command_completed" in types
        assert "core.model_turn" in types


def test_p2a_confirmation_lifecycle_observes_state_and_version(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path)
    with core:
        # --- Case 1: Deny preserves state unchanged ---
        core.create_session("sess-deny")
        res_deny_req = core.invoke_command("sess-deny", "confirm_action", {}, expected_revision=core.command_catalog().revision_id)
        assert res_deny_req.status == "pending_confirmation"
        assert res_deny_req.confirmation is not None
        req_id_deny = res_deny_req.confirmation.request_id

        core.confirm_action("sess-deny", req_id_deny, "deny", expected_revision=core.command_catalog().revision_id)
        # Verify from public read command: state is UNCHANGED
        check_deny = core.invoke_command("sess-deny", "hello_get", {}, expected_revision=core.command_catalog().revision_id)
        assert check_deny.data is not None
        assert check_deny.data["action_done"] is False
        assert check_deny.data["version"] == 0

        # --- Case 2: Stale detection when state version bumped ---
        core.create_session("sess-stale")
        res_stale_req = core.invoke_command("sess-stale", "confirm_action", {}, expected_revision=core.command_catalog().revision_id)
        assert res_stale_req.confirmation is not None
        req_id_stale = res_stale_req.confirmation.request_id

        # Bump state version in between!
        core.invoke_command("sess-stale", "hello_set", {"greeting": "bumped"}, expected_revision=core.command_catalog().revision_id)
        check_bumped = core.invoke_command("sess-stale", "hello_get", {}, expected_revision=core.command_catalog().revision_id)
        assert check_bumped.data is not None
        assert check_bumped.data["version"] == 1

        # Trying to confirm now MUST raise stale error
        with pytest.raises(ValueError, match="stale"):
            core.confirm_action("sess-stale", req_id_stale, "allow", expected_revision=core.command_catalog().revision_id)

        # --- Case 3: Allow applies action under user actor and updates state ---
        core.create_session("sess-allow")
        res_allow_req = core.invoke_command("sess-allow", "confirm_action", {}, expected_revision=core.command_catalog().revision_id)
        assert res_allow_req.confirmation is not None
        req_id_allow = res_allow_req.confirmation.request_id

        core.confirm_action("sess-allow", req_id_allow, "allow", expected_revision=core.command_catalog().revision_id)
        check_allowed = core.invoke_command("sess-allow", "hello_get", {}, expected_revision=core.command_catalog().revision_id)
        assert check_allowed.data is not None
        assert check_allowed.data["action_done"] is True
        assert check_allowed.data["version"] == 1

        # Every confirmation lifecycle step is a durable fact a client can project from,
        # bound to the revision that produced it.
        def _confirmation_facts(session_id: str) -> list[tuple[str, dict[str, object]]]:
            return [
                (event.event_type, dict(event.payload))
                for event in core.events_after(session_id)
                if event.event_type.startswith("core.confirmation_")
            ]

        deny_facts = _confirmation_facts("sess-deny")
        assert [kind for kind, _ in deny_facts] == ["core.confirmation_requested", "core.confirmation_decided"]
        assert deny_facts[0][1]["request_id"] == req_id_deny
        assert deny_facts[0][1]["ui_type"] == "confirmation"
        assert deny_facts[1][1] == {"request_id": req_id_deny, "command_name": "confirm_action", "decision": "deny"}
        stale_facts = _confirmation_facts("sess-stale")
        assert stale_facts[-1][0] == "core.confirmation_decided"
        assert stale_facts[-1][1]["decision"] == "stale"
        allow_facts = _confirmation_facts("sess-allow")
        assert [fact[1].get("decision") for fact in allow_facts if fact[0] == "core.confirmation_decided"] == ["allow"]


def test_p2a_durable_continuation_executes_real_governed_run_and_recovers(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path, include_provider=True)
    with core:
        core.create_session("sess-cont")

        # hello_set enqueues a durable continuation
        core.invoke_command("sess-cont", "hello_set", {"greeting": "automated"}, expected_revision=core.command_catalog().revision_id)

        # Drain executes real governed run with provider
        results = core.drain_continuations("sess-cont", limit=10)
        assert len(results) == 1
        assert results[0].status == "completed"
        assert results[0].owner == "hello-state"
        assert results[0].payload.get("action") == "greet_followup"
        assert results[0].payload.get("greeting") == "automated"

        # Draining again yields 0 continuations (already drained, no-op prevention)
        empty_drain = core.drain_continuations("sess-cont", limit=10)
        assert len(empty_drain) == 0

        # Check continuation run facts recorded in event stream
        events = core.events_after("sess-cont", after_sequence=0)
        event_types = [e.event_type for e in events]
        assert "core.continuation_started" in event_types
        assert "core.model_turn" in event_types


def test_p2a_usage_windowing_and_unknown_semantics(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path, include_provider=True)
    with core:
        core.create_session("sess-usage")

        # 1. Initial usage
        u0 = core.usage("sess-usage")
        assert u0.input_tokens == 0
        assert u0.output_tokens == 0
        assert u0.has_unknown_usage is False
        assert u0.turn_count == 0

        # 2. Run real provider turn (LoopbackProvider reports 100 in, 50 out, 20 cache read, 80 uncached)
        core.run("first turn", session_id="sess-usage")
        u1 = core.usage("sess-usage")
        assert u1.input_tokens == 100
        assert u1.output_tokens == 50
        assert u1.cache_read_tokens == 20
        assert u1.uncached_input_tokens == 80
        assert u1.has_unknown_usage is False
        assert u1.turn_count == 1
        first_seq = u1.through_sequence

        # 3. Running a command afterwards preserves usage as known
        core.invoke_command("sess-usage", "hello_set", {"greeting": "test"}, expected_revision=core.command_catalog().revision_id)
        u2 = core.usage("sess-usage")
        assert u2.input_tokens == 100
        assert u2.has_unknown_usage is False

        # 4. If provider turn fails/interrupted without usage, has_unknown_usage is True
        # Insert provider_interrupted
        latest_s = core.store.session("sess-usage")
        core.store.commit(
            "sess-usage",
            expected_sequence=latest_s.last_event_sequence,
            expected_run_id=None,
            run_id="run-err",
            revision_id=core.revision_id,
            events=(
                EventDraft(
                    "core.provider_intent",
                    "kernel",
                    "kernel",
                    {"attempt": 1, "start_time_ms": 1000},
                ),
                EventDraft(
                    "core.provider_interrupted",
                    "kernel",
                    "kernel",
                    {"dispatch_sequence": latest_s.last_event_sequence + 1, "error": "simulated transport loss"},
                ),
            ),
            status="interrupted",
            checkpoint_kind="interrupted",
            checkpoint_payload={},
        )

        u3 = core.usage("sess-usage")
        assert u3.has_unknown_usage is True

        # Windowed usage strictly before the interruption evaluates ONLY the first turn
        u_windowed = core.usage("sess-usage", since_sequence=0, until_sequence=first_seq)
        assert u_windowed.input_tokens == 100
        assert u_windowed.has_unknown_usage is False


def test_declarative_client_syntax_reaches_the_catalog(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path, extra_command=True)
    with core:
        catalog = {command.name: command for command in core.command_catalog().commands}
        assert catalog["shell_like"].client_syntax == "!"
        assert catalog["hello_set"].client_syntax is None

        core.create_session("sess-syntax")
        result = core.invoke_command("sess-syntax", "shell_like", {}, expected_revision=core.revision_id)
        assert result.status == "ok"
        events = [event for event in core.events_after("sess-syntax") if event.event_type == "core.command_completed"]
        assert events and events[0].actor == "client"


def test_read_only_policy_denies_write_commands_uniformly(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path, extra_command=True, read_only=True)
    with core:
        core.create_session("sess-read-only")
        denied = core.invoke_command("sess-read-only", "hello_set", {"greeting": "x"}, expected_revision=core.revision_id)
        assert denied.status == "error"
        assert "read-only" in (denied.error or "")
        shell_denied = core.invoke_command("sess-read-only", "shell_like", {}, expected_revision=core.revision_id)
        assert shell_denied.status == "error"
        assert "read-only" in (shell_denied.error or "")


def test_p2a_reserved_command_name_rejection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"

    root = make_p2a_plugin(tmp_path, plugin_id="rogue-plugin", reserved_command="run")
    plugin_source = PluginSource(root=root, plugin_id="rogue-plugin", config={})
    with pytest.raises(Exception, match="reserved command name or prefix"):
        with Core(
            workspace=workspace,
            state_dir=state_dir,
            config=ExecutionConfig(),
            plugins=(plugin_source,),
            revision_id=uuid.uuid4().hex,
            revision_payload={"test": 1},
        ):
            pass

    root2 = make_p2a_plugin(tmp_path, plugin_id="rogue-plugin-2", reserved_command="core.test")
    plugin_source2 = PluginSource(root=root2, plugin_id="rogue-plugin-2", config={})
    with pytest.raises(Exception, match="reserved command name or prefix"):
        with Core(
            workspace=workspace,
            state_dir=state_dir,
            config=ExecutionConfig(),
            plugins=(plugin_source2,),
            revision_id=uuid.uuid4().hex,
            revision_payload={"test": 2},
        ):
            pass


def test_p2a_busy_gate_and_waiting_session_lifecycle(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path, include_context=True, include_observer=True, include_provider=True)
    with core:
        core.create_session("sess-busy")

        # 1. Real drive to waiting via tool approval
        run_res = core.run("need_approval", session_id="sess-busy")
        assert run_res.status == "waiting"
        sess_waiting = core.session("sess-busy")
        assert sess_waiting.status == "waiting"

        # 2. Gated command MUST be rejected during waiting status
        with pytest.raises(ValueError, match="is busy and command 'gated_action' requires an idle session"):
            core.invoke_command("sess-busy", "gated_action", {}, expected_revision=core.command_catalog().revision_id)

        # 3. Non-gated command succeeds during waiting status
        set_res = core.invoke_command("sess-busy", "hello_set", {"greeting": "during-wait"}, expected_revision=core.command_catalog().revision_id)
        assert set_res.status == "ok"

        # Verify an event was committed after the parked checkpoint
        events = core.events_after("sess-busy")
        assert any(e.event_type == "core.command_completed" for e in events)

        # 4. Resume via approval decision across intervening command
        app_ev = next(e for e in events if e.event_type == "core.approval_requested")
        req_id = app_ev.payload["request_id"]

        resume_res = core.resume("sess-busy", approval_request_id=req_id, decision="allow")
        assert resume_res.status == "completed"
        assert resume_res.output == "done after approval"

        sess_done = core.session("sess-busy")
        assert sess_done.status == "completed"

        # The completed governed invocation reached the observer with its owner and the
        # declared effects the core actually enforced.
        observed = core.invoke_command("sess-busy", "hello_get", {}, expected_revision=core.command_catalog().revision_id)
        assert observed.data is not None
        assert observed.data["observed_owner"] == "hello-state"
        assert tuple(observed.data["observed_effects"]) == ("write",)


def test_p2a_observer_failure_is_diagnostic_not_fatal(tmp_path: Path) -> None:
    core, _ = make_configured_core(tmp_path, include_observer=True, observer_raises=True, include_provider=True)
    with core:
        core.create_session("sess-obs-err")

        run_res = core.run("trigger observation", session_id="sess-obs-err")
        assert run_res.status == "completed"

        events = core.events_after("sess-obs-err", after_sequence=0)
        types = [e.event_type for e in events]

        # The observed fact stays durable; the failure is a diagnostic event
        assert "core.model_turn" in types
        assert "core.run_completed" in types
        err_events = [e for e in events if e.event_type == "core.observer_error"]
        assert err_events, "observer failure must surface as core.observer_error"
        assert err_events[0].payload["owner"] == "hello-state"
        assert "observer exploded" in str(err_events[0].payload["error"])
