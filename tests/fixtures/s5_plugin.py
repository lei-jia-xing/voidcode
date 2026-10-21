"""Fixture plugin source for the P4 S5 kernel/SDK seams.

`s5_probe` (READ) and `s5_write` (WRITE) share one generic action surface, so the same seam can
be driven under either lease stance: this session's own durable facts (`session_info`), one
child's committed events (`read_child_events`), nested governed work (`invoke_tool`, `recurse`),
a real file mutation (`write`), one file version with the coverage claim a read tool makes
(`observe`), the invocation context a tool was handed (`coverage_context`), mid-invocation
progress (`progress`), and a command (`s5_command`) that asks for the same nested work through
its own dispatch. Every action answers with a real `ToolSuccess` whose `OpaqueToolBody` carries
the action's JSON, which is also its model-visible `TextOutput` text.
"""

from pathlib import Path

PLUGIN_ID = "s5-fixture"
PROBE_TOOL = "s5_probe"
WRITE_TOOL = "s5_write"
COMMAND_NAME = "s5_command"
#: Diagnostic a hook writes when the kernel refuses its nested invocation attempt.
HOOK_DIAGNOSTIC = "hook_refusal.txt"


def write_s5_plugin(
    source: Path,
    *,
    plugin_id: str = PLUGIN_ID,
    hooks: bool = False,
) -> Path:
    """Write (or rewrite) the plugin source directory; returns the source."""
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text(_MODULE_CODE.replace("__HOOKS__", repr(hooks)).replace("__PLUGIN_ID__", plugin_id), encoding="utf-8")
    return source


_MODULE_CODE = r'''"""Generated S5 fixture: session facts, child reads, coverage and nested work."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.commands import CommandDefinition, CommandResult
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import OpaqueToolBody, OutputBounds, ReadObservation, TextOutput, ToolDefinition, ToolEffect, ToolFailure, ToolSuccess

PROBE_TOOL = "s5_probe"
WRITE_TOOL = "s5_write"
COMMAND_NAME = "s5_command"
HOOK_DIAGNOSTIC = "hook_refusal.txt"
#: How long `progress` and a waiting `write` hold their invocation open for the caller.
PROGRESS_WAIT_SECONDS = 60.0


def _session_info(services):
    return {"session": services.session_info().as_payload()}


def _child_events(services, call):
    """One bounded child read; a refusal is the kernel's, so the fixture reports it verbatim."""
    child_session_id = str(call.arguments.get("child_session_id", ""))
    after_sequence = int(call.arguments.get("after_sequence", 0))
    limit = int(call.arguments.get("limit", 100))
    include_payload = bool(call.arguments.get("include_payload", True))
    body = {
        "child_session_id": child_session_id,
        "after_sequence": after_sequence,
        "limit": limit,
        "include_payload": include_payload,
    }
    try:
        views = services.read_child_events(
            child_session_id,
            after_sequence=after_sequence,
            limit=limit,
            include_payload=include_payload,
        )
    except Exception as exc:
        body["refused"] = f"{type(exc).__name__}: {exc}"
        return body
    body["views"] = [view.as_payload() for view in views]
    return body


def _invoke(services, context, name, arguments, expected_revision=None):
    """One nested invocation; the kernel's receipt is the whole answer the caller gets here."""
    revision = context.revision_id if expected_revision is None else expected_revision
    return services.invoke_tool(name, arguments, expected_revision=revision).as_payload()


def _invoke_tool(services, context, arguments):
    """The nested-request action, shared by a tool call and a command invocation."""
    name = str(arguments.get("name", ""))
    raw = arguments.get("arguments")
    nested = json_wire_object(raw) if isinstance(raw, Mapping) else {}
    expected = arguments.get("expected_revision")
    body = {"name": name, "arguments": nested, "invocation_depth": context.invocation_depth}
    try:
        body["receipt"] = _invoke(services, context, name, nested, None if expected is None else str(expected))
    except Exception as exc:
        body["refused"] = f"{type(exc).__name__}: {exc}"
        return body
    if arguments.get("raise_after_stage"):
        # A callback that dies after asking for nested work: the request is already staged, so
        # what the kernel does with it is exactly what this action has to show.
        raise RuntimeError("fixture failed after staging")
    return body


def _recurse(services, context, call):
    """Ask for one more of ourselves: the kernel owns the depth and chain-action bounds."""
    body = {"invocation_depth": context.invocation_depth}
    try:
        body["receipt"] = _invoke(services, context, PROBE_TOOL, {"action": "recurse"})
    except Exception as exc:
        body["refused"] = f"{type(exc).__name__}: {exc}"
    return body


def _wait_for_release(context):
    """Hold this invocation open until the caller drops the release file (bounded, abort-aware)."""
    release = Path(context.workspace) / "release"
    deadline = time.monotonic() + PROGRESS_WAIT_SECONDS
    while not release.exists() and time.monotonic() < deadline:
        if context.abort_signal is not None and context.abort_signal.cancelled:
            return "cancelled"
        time.sleep(0.02)
    return "released" if release.exists() else "timeout"


def _write(context, call):
    """Mutate one workspace file; a caller can hold the invocation open after the write lands."""
    path_text = str(call.arguments.get("path", ""))
    target = Path(context.workspace) / path_text
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(call.arguments.get("text", "")), encoding="utf-8")
    body = {"path": path_text, "bytes": target.stat().st_size}
    if call.arguments.get("wait_after_write"):
        body["waited"] = _wait_for_release(context)
    return body


def _observe(context, call):
    """Show one file version the way a read tool does, and claim exactly what it showed."""
    path_text = str(call.arguments.get("path", ""))
    raw = (Path(context.workspace) / path_text).read_bytes()
    source_lines = raw.decode("utf-8").splitlines()
    total = len(source_lines)
    start = max(1, int(call.arguments.get("start") or 1))
    end = total if call.arguments.get("end") is None else int(call.arguments["end"])
    window = source_lines[start - 1:max(0, end)] if total else []
    text = "\n".join(window)
    whole_file = start <= 1 and end >= total
    clipped = bool(call.arguments.get("clipped", False))
    body = {
        "path": path_text,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "lines": list(range(start, start + len(window))),
        "line_count": total,
        "whole_file": whole_file,
        "clipped": clipped,
        "bytes": len(text.encode("utf-8")),
    }
    output_bytes = int(call.arguments.get("output_bytes") or 0)
    if output_bytes > len(text.encode("utf-8")):
        text = text + "\n" + "x" * (output_bytes - len(text.encode("utf-8")) - 1)
    return ToolSuccess(
        call.tool_name,
        TextOutput(
            text,
            bounds=OutputBounds(
                truncated=bool(call.arguments.get("truncated", False)),
                partial=bool(call.arguments.get("partial", False)),
            ),
        ),
        OpaqueToolBody(body),
        coverage=(
            ReadObservation(
                path=path_text,
                content_hash=body["sha256"],
                lines=tuple(body["lines"]),
                line_count=total,
                whole_file=whole_file,
                clipped=clipped,
            ),
        ),
    )


def _coverage_context(context):
    """Every fact the kernel handed this invocation, as the plugin sees it."""
    return {
        "workspace": str(context.workspace),
        "session_id": context.session_id,
        "run_id": context.run_id,
        "invocation_id": context.invocation_id,
        "revision_id": context.revision_id,
        "actor": context.actor,
        "logical_turn": context.logical_turn,
        "invocation_depth": context.invocation_depth,
        "read_paths": sorted(context.read_paths),
        "read_lines": [
            {"path": path, "content_hash": sha, "lines": sorted(lines)}
            for (path, sha), lines in sorted(context.read_lines.items())
        ],
        "read_whole_files": [[path, sha] for path, sha in sorted(context.read_whole_files)],
        "read_hash": context.read_hash,
        "has_progress": context.emit_tool_progress is not None,
    }


def _progress(context):
    """Report one line of progress, then hold the invocation open until the caller releases it."""
    if context.emit_tool_progress is None:
        return {"progress": "unavailable"}
    context.emit_tool_progress({"stream": "stdout", "text": "ready"})
    return {"progress": _wait_for_release(context)}


def _dispatch(action, call, context, services):
    if action == "session_info":
        return _session_info(services)
    if action == "child_events":
        return _child_events(services, call)
    if action == "invoke_tool":
        return _invoke_tool(services, context, call.arguments)
    if action == "recurse":
        return _recurse(services, context, call)
    if action == "write":
        return _write(context, call)
    if action == "observe":
        return _observe(context, call)
    if action == "coverage_context":
        return _coverage_context(context)
    if action == "progress":
        return _progress(context)
    if action == "echo":
        return {"echo": str(call.arguments.get("text", ""))}
    return None


class _Tool:
    """One fixture tool with the whole action surface; both tools share this dispatch."""

    def invoke(self, call, *, context, services):
        action = str(call.arguments.get("action", ""))
        if action == "fail":
            return ToolFailure(call.tool_name, str(call.arguments.get("text") or "fixture failure"))
        result = _dispatch(action, call, context, services)
        if result is None:
            return ToolFailure(call.tool_name, f"unknown action: {action}")
        if isinstance(result, (ToolSuccess, ToolFailure)):
            return result
        return ToolSuccess(call.tool_name, TextOutput(json.dumps(result, sort_keys=True)), OpaqueToolBody(result))


class SessionProbe(_Tool):
    """Read-only: this session's own facts, its child's events, coverage and nested read work."""

    definition = ToolDefinition(
        name=PROBE_TOOL,
        description="Read this session's own facts, one child's events, or one file version.",
        input_schema={"type": "object", "additionalProperties": True},
        effects=frozenset({ToolEffect.READ}),
        path_argument_keys=("path",),
    )


class SessionWrite(_Tool):
    """Writable: the same actions, plus a real workspace mutation."""

    definition = ToolDefinition(
        name=WRITE_TOOL,
        description="Drive the same actions from a writable lease, mutating the workspace.",
        input_schema={"type": "object", "additionalProperties": True},
        effects=frozenset({ToolEffect.WRITE}),
    )


class InvokeCommand:
    """The command-side seam: the same nested request, asked from a command's own context."""

    definition = CommandDefinition(
        name=COMMAND_NAME,
        description="Ask the kernel for one nested tool invocation (the command-side seam).",
        argument_schema={"type": "object", "additionalProperties": True},
    )

    def invoke(self, invocation, services):
        body = _invoke_tool(services, invocation.context, invocation.arguments)
        output = json.dumps(body, sort_keys=True)
        if "receipt" in body:
            return CommandResult(status="ok", output=output, data=body)
        return CommandResult(status="error", output=output, error=str(body["refused"]))


def _pre_tool(intent, context, services):
    """A gate answers "may this run?"; it denies only a caller that asked it to.

    A hook lease observes the call it gates and may start no nested governed work; when the call
    asks for that attempt the fixture records what the kernel answered, in its own file.
    """
    if intent.arguments.get("gate_denied"):
        return f"{intent.tool_name} refused by the s5 fixture gate"
    if intent.arguments.get("hook_nested_probe"):
        try:
            services.invoke_tool(PROBE_TOOL, {"action": "echo", "text": "from a hook"}, expected_revision=context.revision_id)
            note = "unexpectedly allowed"
        except Exception as exc:
            note = f"{type(exc).__name__}: {exc}"
        (Path(context.workspace) / HOOK_DIAGNOSTIC).write_text(note, encoding="utf-8")
    return None


def _post_tool(outcome, context, services):
    """One note from a settled invocation: the session facts it left behind."""
    info = services.session_info().as_payload()
    return json.dumps(
        {
            "hook": "post_tool",
            "tool": outcome.tool_name,
            "session_id": info["session_id"],
            "status": info["status"],
            "pending_approval": info["pending_approval"] is not None,
            "chain_budget": info["chain_budget"],
        },
        sort_keys=True,
    )


_HOOKS = __HOOKS__


def create_plugin(config):
    hooks = _HOOKS
    return Plugin(
        tools=(SessionProbe(), SessionWrite()),
        commands=(InvokeCommand(),),
        pre_tool=_pre_tool if hooks else None,
        pre_tool_match=(PROBE_TOOL, WRITE_TOOL) if hooks else ("*",),
        post_tool=_post_tool if hooks else None,
        post_tool_match=(PROBE_TOOL, WRITE_TOOL) if hooks else ("*",),
    )
'''
