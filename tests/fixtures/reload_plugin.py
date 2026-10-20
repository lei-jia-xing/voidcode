"""Fixture plugin source whose revision, state schema, and behavior can be rewritten in place.

Used by the P3 activation tests: the same plugin id is admitted as v1 and later re-admitted as
v2 with different tool output, a higher state schema version, and controlled migration or
deactivation behavior.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path

from voidcode.sdk.manifest import (
    PluginCapabilityRequest,
    PluginCommandDeclaration,
    PluginFileIdentity,
    PluginHookDeclaration,
    PluginManifest,
    PluginToolDeclaration,
)
from voidcode.sdk.tools import ToolEffect

PROBE_TOOL = "reload_probe"
DEACTIVATE_TOOL = "reload_state_note"
BLOCK_TOOL = "reload_block"
DELEGATE_TOOL = "reload_delegate"
SERVICES_TOOL = "reload_services"
SERVICES_READ_TOOL = "reload_services_read"
PROCESS_TOOL = "reload_process"
PROCESS_READ_TOOL = "reload_process_read"
OVERLAY_COMMAND = "reload_overlay"


def write_reload_plugin(
    source: Path,
    *,
    plugin_id: str = "reload-fixture",
    version: str = "1.0.0",
    tool_output: str = "v1",
    module_name: str = "reload_plugin_impl",
    state_schema_version: int = 1,
    state_schema: dict | None = None,
    migration: str = "none",
    deactivation: str = "none",
    observe: bool = False,
    depends: tuple[str, ...] = (),
    syntax_error: bool = False,
    tools: bool = True,
    marker: Path | None = None,
    factory_error: bool = False,
    blocking: bool = False,
    delegating: bool = False,
    services_probe: bool = False,
    services_read_probe: bool = False,
    process_tool: bool = False,
    overlay_command: bool = False,
    post_tool_hook: bool = False,
    post_tool_match: tuple[str, ...] = (),
    post_tool_stage: bool = False,
    post_tool_process: bool = False,
    post_tool_spill: bool = False,
    pre_tool_gate: str = "",
    pre_tool_match: tuple[str, ...] = (),
    lifecycle_hooks: bool = False,
) -> Path:
    """Write (or rewrite) the plugin source directory and return it."""
    source.mkdir(parents=True, exist_ok=True)
    # A stale manifest would hide an unchanged source from the revision identity.
    (source / "voidcode-plugin.json").write_text("{}", encoding="utf-8")
    body = _plugin_code(
        plugin_id=plugin_id,
        tool_output=tool_output,
        module_name=module_name,
        migration=migration,
        deactivation=deactivation,
        observe=observe,
        marker=marker,
        factory_error=factory_error,
        blocking=blocking,
        delegating=delegating,
        services_probe=services_probe,
        services_read_probe=services_read_probe,
        process_tool=process_tool,
        overlay_command=overlay_command,
        post_tool_hook=post_tool_hook,
        post_tool_match=post_tool_match,
        post_tool_stage=post_tool_stage,
        post_tool_process=post_tool_process,
        post_tool_spill=post_tool_spill,
        pre_tool_gate=pre_tool_gate,
        pre_tool_match=pre_tool_match,
        lifecycle_hooks=lifecycle_hooks,
    )
    module_path = source / f"{module_name}.py"
    module_path.write_text(body, encoding="utf-8")
    if syntax_error:
        module_path.write_text(body + "\nthis is not python(\n", encoding="utf-8")
    (source / "__init__.py").write_text("", encoding="utf-8")

    files = (
        PluginFileIdentity(path="__init__.py", sha256=hashlib.sha256(b"").hexdigest()),
        PluginFileIdentity(path=f"{module_name}.py", sha256=hashlib.sha256(module_path.read_bytes()).hexdigest()),
    )
    declarations: tuple[PluginToolDeclaration, ...] = ()
    if tools and blocking:
        declarations += (
            PluginToolDeclaration(
                name=BLOCK_TOOL,
                description="Block until this session is cancelled.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effects=(ToolEffect.READ,),
            ),
        )
    if tools and delegating:
        declarations += (
            PluginToolDeclaration(
                name=DELEGATE_TOOL,
                description="Spawn delegated child runs and join them.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompts": {"type": "array", "items": {"type": "string"}},
                        "allowlist": {"type": "array", "items": {"type": "string"}},
                        "readonly": {"type": "boolean"},
                    },
                    "required": ["prompts"],
                    "additionalProperties": False,
                },
                effects=(ToolEffect.SPAWN,),
            ),
        )
    if tools and process_tool:
        process_schema = {
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}},
                "stdin": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "max_output_bytes": {"type": "integer"},
            },
            "required": ["argv"],
            "additionalProperties": False,
        }
        declarations += (
            PluginToolDeclaration(
                name=PROCESS_TOOL,
                description="Run a child process under this lease.",
                input_schema=process_schema,
                effects=(ToolEffect.EXECUTE,),
            ),
            PluginToolDeclaration(
                name=PROCESS_READ_TOOL,
                description="Attempt to run a child process from a read-only lease.",
                input_schema=process_schema,
                effects=(ToolEffect.READ,),
            ),
        )
    if tools and services_probe:
        declarations += (
            PluginToolDeclaration(
                name=SERVICES_TOOL,
                description="Exercise the plugin service seams (wakeup/events/model_query).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "text": {"type": "string"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                        "include_payload": {"type": "boolean"},
                        "media_type": {"type": "string"},
                        "sha256": {"type": "string"},
                        "repeat": {"type": "integer"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "process_id": {"type": "string"},
                        "stream": {"type": "string"},
                        "stdin": {"type": "boolean"},
                        "stdout_suffix": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                effects=(ToolEffect.WRITE,),
            ),
        )
    if tools and services_read_probe:
        declarations += (
            PluginToolDeclaration(
                name=SERVICES_READ_TOOL,
                description="Exercise the plugin service seams from a read-only lease.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "text": {"type": "string"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                        "include_payload": {"type": "boolean"},
                        "media_type": {"type": "string"},
                        "sha256": {"type": "string"},
                        "repeat": {"type": "integer"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "process_id": {"type": "string"},
                        "stream": {"type": "string"},
                        "stdin": {"type": "boolean"},
                        "stdout_suffix": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
                effects=(ToolEffect.READ,),
            ),
        )
    if tools:
        declarations += (
            PluginToolDeclaration(
                name=PROBE_TOOL,
                description=f"Probe the loaded revision ({tool_output}).",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effects=(ToolEffect.WRITE,),
            ),
            PluginToolDeclaration(
                name=DEACTIVATE_TOOL,
                description="Read the owner state this revision loaded.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effects=(ToolEffect.READ,),
            ),
        )
    command_declarations: tuple[PluginCommandDeclaration, ...] = ()
    if overlay_command:
        command_declarations = (
            PluginCommandDeclaration(
                name=OVERLAY_COMMAND,
                description="Enter or leave the restrictive overlay (user action).",
                argument_schema={
                    "type": "object",
                    "properties": {"action": {"enum": ["on", "off"]}},
                    "required": ["action"],
                    "additionalProperties": False,
                },
                effects=(ToolEffect.SESSION,),
                direct_user=True,
                client_syntax="/plan",
            ),
        )
    hook_declarations: list[PluginHookDeclaration] = []
    if post_tool_hook:
        hook_declarations.append(PluginHookDeclaration(kind="post_tool", match=tuple(post_tool_match or (PROBE_TOOL,)), mutating=True))
    if pre_tool_gate:
        hook_declarations.append(PluginHookDeclaration(kind="pre_tool", match=tuple(pre_tool_match or (PROBE_TOOL,)), mutating=False))
    if lifecycle_hooks:
        hook_declarations.append(PluginHookDeclaration(kind="session_start"))
        hook_declarations.append(PluginHookDeclaration(kind="session_end"))
    manifest = PluginManifest(
        plugin_id=plugin_id,
        version=version,
        entrypoint=f"{module_name}:create_plugin",
        files=files,
        capabilities=(PluginCapabilityRequest(name="workspace.write", reason="fixture probe"),),
        tools=declarations,
        commands=command_declarations,
        hooks=tuple(hook_declarations),
        depends=depends,
        state_schema_version=state_schema_version,
        state_schema=state_schema,
        observed_facts=("invocation_finished",) if observe else (),
    )
    (source / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")
    return source


def _indent(body: str) -> str:
    """Indent a generated hook body to one level inside its function definition."""
    return "\n".join(("    " + line) if line.strip() else line for line in body.splitlines())


def _plugin_code(
    *,
    plugin_id: str,
    tool_output: str,
    module_name: str,
    migration: str,
    deactivation: str,
    observe: bool,
    marker: Path | None,
    factory_error: bool,
    blocking: bool,
    delegating: bool,
    services_probe: bool,
    services_read_probe: bool,
    process_tool: bool,
    overlay_command: bool,
    post_tool_hook: bool,
    post_tool_match: tuple[str, ...],
    post_tool_stage: bool,
    post_tool_process: bool,
    post_tool_spill: bool,
    pre_tool_gate: str,
    pre_tool_match: tuple[str, ...],
    lifecycle_hooks: bool,
) -> str:
    marker_repr = "" if marker is None else str(marker)
    hang_body = (
        "import time\nfrom pathlib import Path\n"
        f"Path({marker_repr!r}).write_text('migrating')\n"
        "deadline = time.time() + 60\n"
        "while time.time() < deadline:\n"
        "    time.sleep(0.05)\n"
        "return state"
    )
    migrate_body = {
        "none": "return None",
        "ok": 'migrated = dict(state)\nmigrated["migrated_to"] = to_version\nreturn migrated',
        "raise": 'raise RuntimeError("migration refused by the candidate")',
        # A parametrized refusal: ValueError text is forwarded to the user verbatim.
        "refuse": 'raise ValueError("candidate refuses to migrate this state")',
        "crash": "import os\nos._exit(9)",
        "invalid": "return {'migrated_to': 'not-an-int' if True else 0, 'note': [1, 2]}",
        # Blocks (bounded) inside the candidate host so a test can kill the kernel at a
        # deterministic point before the activation transaction commits.
        "hang": hang_body,
    }[migration]
    deactivate_body = {
        "none": "return None",
        "pause": 'paused = dict(state)\npaused["paused"] = True\npaused["tip"] = tip_sequence\nreturn paused',
        "raise": 'raise RuntimeError("deactivation refused")',
    }[deactivation]
    observer_body = (
        textwrap.dedent(
            """
            def _observe(fact, context, services):
                state, version = services.get_state("workspace")
                merged = dict(state or {})
                merged["observed"] = merged.get("observed", 0) + 1
                services.replace_state(merged, expected_version=version, scope="workspace")
                return None
            """
        ).strip()
        if observe
        else ""
    )
    return f'''"""Generated reload fixture for plugin {plugin_id} (output {tool_output})."""

from __future__ import annotations

import json
import os
from pathlib import Path

from voidcode.sdk.plugin import Plugin, tool_definition
from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.tools import TextOutput, ToolFailure, ToolSuccess, ToolEffect

OUTPUT = {tool_output!r}


def _spawn(services, call, log_dir, suffix):
    """Start one supervised process whose logs the caller names inside the workspace."""
    return services.spawn_process(
        [str(item) for item in (call.arguments.get("argv") or ())],
        stdin=bool(call.arguments.get("stdin", False)),
        stdout_path=log_dir / f"{{suffix}}.log",
        stderr_path=log_dir / f"{{suffix}}.err",
        process_id=call.arguments.get("process_id"),
    )


class Probe:
    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        state, version = services.get_state("session")
        merged = dict(state or {{}})
        merged["note"] = OUTPUT
        services.replace_state(merged, expected_version=version, scope="session")
        return ToolSuccess(call.tool_name, TextOutput(f"probe={{OUTPUT}}"))


class ReadNote:
    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        state, _version = services.get_state("session")
        return ToolSuccess(call.tool_name, TextOutput(json.dumps(dict(state or {{}}), sort_keys=True)))


class ServicesProbe:
    """Drive one plugin-service seam per call and return its observable result."""

    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        action = str(call.arguments.get("action", ""))
        text = str(call.arguments.get("text", ""))
        if action == "wakeup":
            wakeup_id = services.schedule_wakeup({{"text": text}}, due_at_ms=0, idempotency_key=text, role="user")
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"wakeup_id": wakeup_id}})))
        if action == "events":
            limit = int(call.arguments.get("limit", 100))
            include_payload = bool(call.arguments.get("include_payload", True))
            views = services.read_events(limit=limit, include_payload=include_payload)
            return ToolSuccess(
                call.tool_name,
                TextOutput(
                    json.dumps(
                        {{
                            "count": len(views),
                            "types": [view.event_type for view in views],
                            "payloads": [view.payload is not None for view in views],
                            "session": context.session_id,
                        }},
                        sort_keys=True,
                    )
                ),
            )
        if action == "project":
            events = services.read_events(after_sequence=0, limit=200)
            # Cover only a batch-complete prefix: the boundary is the last committed message or
            # answered batch, never the in-flight invocation this call belongs to.
            boundaries = [
                event.sequence
                for event in events
                if event.event_type in ("kernel.user_message", "kernel.continuation_started", "kernel.wakeup_fired", "kernel.tool_completed")
                and event.sequence < events[-1].sequence
            ]
            services.project_window(
                covered_from_sequence=events[0].sequence,
                covered_to_sequence=boundaries[-1],
                replacement="summary of the earlier conversation",
            )
            return ToolSuccess(call.tool_name, TextOutput("projected"))
        if action == "project_huge":
            events = services.read_events(after_sequence=0, limit=200)
            services.project_window(
                covered_from_sequence=events[0].sequence,
                covered_to_sequence=events[0].sequence,
                replacement="x" * 100000,
            )
            return ToolSuccess(call.tool_name, TextOutput("projected"))
        if action == "overlay":
            services.set_overlay("read-only")
            return ToolSuccess(call.tool_name, TextOutput("overlay set"))
        if action == "query":
            answer = services.model_query(text)
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"answer": answer}})))
        if action == "spill":
            media_type = str(call.arguments.get("media_type") or "text/plain; charset=utf-8")
            # `repeat` keeps large payloads out of the model-visible arguments.
            payload = text * max(1, int(call.arguments.get("repeat", 1)))
            reference = services.spill_artifact(payload, media_type=media_type)
            return ToolSuccess(call.tool_name, TextOutput(json.dumps(reference.as_payload(), sort_keys=True)))
        if action == "page":
            try:
                page = services.read_artifact(
                    str(call.arguments.get("sha256", "")),
                    offset=int(call.arguments.get("offset", 0)),
                    limit=int(call.arguments.get("limit", 65536)),
                )
            except Exception as exc:
                # The plugin chooses to show the refusal; the host never leaks messages itself.
                return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}})))
            return ToolSuccess(
                call.tool_name,
                TextOutput(
                    json.dumps(
                        {{
                            "data": page.text,
                            "bytes": len(page.data),
                            "offset": page.offset,
                            "total_bytes": page.total_bytes,
                            "eof": page.eof,
                            "media_type": page.media_type,
                        }},
                        sort_keys=True,
                    )
                ),
            )
        if action == "bg_spawn":
            log_dir = Path(context.workspace) / "bg"
            suffix = str(call.arguments.get("stdout_suffix") or "out")
            try:
                handle = _spawn(services, call, log_dir, suffix)
            except Exception as exc:
                return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}})))
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"handle": handle.as_payload()}}, sort_keys=True, default=str)))
        if action == "bg_list":
            handles = services.list_processes()
            return ToolSuccess(
                call.tool_name,
                TextOutput(json.dumps({{"handles": [handle.as_payload() for handle in handles]}}, sort_keys=True, default=str)),
            )
        if action == "bg_output":
            try:
                page = services.read_process_output(
                    str(call.arguments.get("process_id", "")),
                    stream=str(call.arguments.get("stream", "stdout")),
                    offset=int(call.arguments.get("offset", 0)),
                    limit=int(call.arguments.get("limit", 65536)),
                )
            except Exception as exc:
                return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}})))
            return ToolSuccess(
                call.tool_name,
                TextOutput(
                    json.dumps(
                        {{
                            "data": page.text,
                            "offset": page.offset,
                            "total_bytes": page.total_bytes,
                            "eof": page.eof,
                            "running": page.running,
                        }},
                        sort_keys=True,
                    )
                ),
            )
        if action == "bg_write":
            try:
                written = services.write_process(str(call.arguments.get("process_id", "")), text)
            except Exception as exc:
                return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}})))
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"written": written}})))
        if action == "bg_cancel":
            try:
                stopped = services.cancel_process(str(call.arguments.get("process_id", "")))
            except Exception as exc:
                return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}})))
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"stopped": stopped}})))
        if action == "svc_spawn":
            try:
                handle = services.spawn_service_process(
                    [str(item) for item in (call.arguments.get("argv") or ())],
                    process_id=call.arguments.get("process_id"),
                )
            except Exception as exc:
                return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}})))
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"handle": handle.as_payload()}}, sort_keys=True, default=str)))
        if action == "svc_wait":
            # One bounded blocking-style wait with active-use binding: poll the service's log with
            # the live lease while honoring the existing abort signal, then stop it on cancel.
            process_id = str(call.arguments.get("process_id", ""))
            deadline = __import__("time").monotonic() + float(call.arguments.get("timeout_seconds", 30))
            offset = 0
            while __import__("time").monotonic() < deadline:
                signal = getattr(context, "abort_signal", None)
                if signal is not None and getattr(signal, "cancelled", False):
                    services.cancel_process(process_id, reason="fixture cancel")
                    return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"cancelled": True}})))
                page = services.read_process_output(process_id, offset=offset, limit=65536)
                offset += len(page.data)
                if page.data:
                    return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"bytes": len(page.data)}})))
                if not page.running:
                    return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"bytes": 0, "running": False}})))
                __import__("time").sleep(0.05)
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"bytes": 0, "timed_out": True}})))
        if action == "spill_abandon":
            # One unterminated chunk: the lease must not leave its private temp file behind.
            import base64 as _b64
            server = services._server
            resp = server._send_reverse_rpc(
                "artifact_spill",
                {{
                    "lease_token": services._lease_token,
                    "spill_id": None,
                    "media_type": "text/plain",
                    "data": _b64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "final": False,
                }},
            )
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"spill_id": resp.get("spill_id", "")}})))
        return ToolFailure(call.tool_name, f"unknown action: {{action}}")


class ProcessProbe:
    """Run one lease-scoped child process and report exactly what the service returned."""

    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        argv = [str(item) for item in (call.arguments.get("argv") or ())]
        stdin = call.arguments.get("stdin")
        timeout = call.arguments.get("timeout_seconds")
        max_output = call.arguments.get("max_output_bytes")
        try:
            result = services.run_process(
                argv,
                stdin=None if stdin is None else str(stdin),
                timeout_seconds=None if timeout is None else float(timeout),
                **{{}} if max_output is None else {{"max_output_bytes": int(max_output)}},
            )
        except Exception as exc:
            return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"refused": f"{{type(exc).__name__}}: {{exc}}"}})))
        return ToolSuccess(
            call.tool_name,
            TextOutput(
                json.dumps(
                    {{
                        "exit_code": result.exit_code,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "truncated": result.truncated,
                        "timed_out": result.timed_out,
                        "cancelled": result.cancelled,
                        "has_duration": result.duration_ms >= 0,
                    }},
                    sort_keys=True,
                )
            ),
        )


class Delegate:
    """Spawn child sessions through the SDK and join them without polling."""

    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        raw_prompts = call.arguments.get("prompts") or ()
        prompts = [str(prompt) for prompt in (raw_prompts if isinstance(raw_prompts, (list, tuple)) else ())]
        raw_allowlist = call.arguments.get("allowlist")
        allowlist = tuple(str(name) for name in raw_allowlist) if isinstance(raw_allowlist, (list, tuple)) else None
        readonly = bool(call.arguments.get("readonly", False))
        children = [
            services.spawn_child(prompt, label=f"child-{{index}}", tool_allowlist=allowlist, readonly=readonly)
            for index, prompt in enumerate(prompts)
        ]
        outcomes = [services.join_child(child_id, timeout_seconds=60).as_payload() for child_id in children]
        return ToolSuccess(call.tool_name, TextOutput(json.dumps({{"children": outcomes}}, sort_keys=True)))


class Block:
    """Hold this session's run inside a tool until the kernel cancels it."""

    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        import time
        from pathlib import Path
        marker = Path(context.workspace) / "block.started"
        marker.write_text(context.session_id, encoding="utf-8")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if context.abort_signal is not None and context.abort_signal.cancelled:
                return ToolFailure(call.tool_name, "block cancelled")
            time.sleep(0.02)
        return ToolFailure(call.tool_name, "block timed out")


def migrate_state(state, from_version, to_version):
{_indent(migrate_body)}


def prepare_deactivate(state, scope, scope_key, tip_sequence):
{_indent(deactivate_body)}
{observer_body}

class OverlayCommand:
    definition = CommandDefinition(
        name="{OVERLAY_COMMAND}",
        description="Enter or leave the restrictive overlay (user action).",
        argument_schema={{
            "type": "object",
            "properties": {{"action": {{"enum": ["on", "off"]}}}},
            "required": ["action"],
            "additionalProperties": False,
        }},
        effects=frozenset({{ToolEffect.SESSION}}),
        direct_user=True,
        client_syntax="/plan",
    )

    def invoke(self, invocation, services):
        action = invocation.arguments.get("action")
        if action == "on":
            services.set_overlay("read-only")
            return CommandResult(status="ok", output="overlay active")
        if action == "off":
            services.clear_overlay()
            return CommandResult(status="ok", output="overlay cleared")
        return CommandResult(status="error", error=f"unknown overlay action: {{action}}")


def _session_start(lifecycle, context, services):
    if lifecycle.phase != "start":
        raise ValueError("session_start hook received a non-start lifecycle point")
    return f"started {{lifecycle.run_id}}"


def _session_end(lifecycle, context, services):
    if lifecycle.phase != "end":
        raise ValueError("session_end hook received a non-end lifecycle point")
    return f"ended {{lifecycle.status}}"


def _pre_tool(intent, context, services):
    if {pre_tool_gate!r} == "explode":
        raise RuntimeError("gate blew up")
    if {pre_tool_gate!r} == "allow":
        return None
    if intent.tool_name == "{PROBE_TOOL}":
        return {pre_tool_gate!r} or None
    return None


def _post_tool(outcome, context, services):
    if outcome.status != "ok":
        raise ValueError("post-tool hooks run only for settled successful results")
    if {post_tool_process!r}:
        try:
            services.run_process(["true"])
        except Exception as exc:
            return f"process refused: {{type(exc).__name__}}"
        return "process allowed"
    if {post_tool_spill!r}:
        try:
            services.spill_artifact("hook spill")
        except Exception as exc:
            return f"spill refused: {{exc}}"
        return "spill allowed"
    if {post_tool_stage!r}:
        services.replace_state({{"touched": [outcome.tool_name]}}, expected_version=0)
        services.stage_event("hook_note", {{"tool": outcome.tool_name}})
    return f"hook note from {{outcome.tool_name}}"


def create_plugin(config):
    if {factory_error!r}:
        raise RuntimeError("fixture factory failure")
    if {str(marker) if marker else ""!r}:
        Path({str(marker) if marker else ""!r}).write_text(str(os.getpid()), encoding="utf-8")
    manifest = parse_plugin_manifest(json.loads(Path(__file__).with_name("voidcode-plugin.json").read_text()))
    tools = tuple(
        Probe(tool_definition(declaration))
        if declaration.name == "{PROBE_TOOL}"
        else Block(tool_definition(declaration))
        if declaration.name == "{BLOCK_TOOL}"
        else Delegate(tool_definition(declaration))
        if declaration.name == "{DELEGATE_TOOL}"
        else ServicesProbe(tool_definition(declaration))
        if declaration.name in ("{SERVICES_TOOL}", "{SERVICES_READ_TOOL}")
        else ProcessProbe(tool_definition(declaration))
        if declaration.name in ("{PROCESS_TOOL}", "{PROCESS_READ_TOOL}")
        else ReadNote(tool_definition(declaration))
        for declaration in manifest.tools
    )
    commands = (OverlayCommand(),) if {overlay_command!r} else ()
    post_tool = _post_tool if {post_tool_hook!r} else None
    pre_tool = _pre_tool if {pre_tool_gate!r} else None
    session_start = _session_start if {lifecycle_hooks!r} else None
    session_end = _session_end if {lifecycle_hooks!r} else None
    return Plugin(
        commands=commands,
        post_tool=post_tool,
        pre_tool=pre_tool,
        session_start=session_start,
        session_end=session_end,
        tools=tools,
        migrate_state=migrate_state if {migration!r} != "none" else None,
        prepare_deactivate=prepare_deactivate if {deactivation!r} != "none" else None,
        observer=_observe if {observe!r} else None,
    )
'''
