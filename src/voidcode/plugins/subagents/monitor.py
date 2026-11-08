"""Running-subagent monitor: a read-only roster plus detail views over core-owned descendants.

The roster and every detail page render through read-only ``command.input``
preparation only; only the explicit subtree cancellation goes through a
governed, confirmed command invocation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar
from uuid import uuid4

from voidcode.sdk.commands import (
    CommandDefinition,
    CommandInvocation,
    CommandPresentation,
    CommandResult,
    ConfirmationRequest,
)
from voidcode.sdk.services import DescendantNode, PluginServices
from voidcode.sdk.tools import ToolEffect

MONITOR_COMMAND = "subagents.monitor"
MONITOR_CANCEL_COMMAND = "subagents.monitor.cancel"
MONITOR_REFRESH_MS = 1000
DETAIL_PAGE_BYTES = 8 * 1024
MAX_DETAIL_EVENTS = 50
MAX_LABEL_CHARS = 160
MAX_ROSTER_LABEL_CHARS = 64


def _short(session_id: str) -> str:
    return session_id[:8]


def _preview(text: str, limit: int = MAX_LABEL_CHARS) -> str:
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


def _node_label(node: DescendantNode, *, indent: str = "") -> str:
    # Whole-roster budget: 64 descendants max (policy max_root_children) times
    # a 64-char row stays far below the 64 KiB presentation contract even with
    # worst-case escaping; status leads so narrow clients see state first.
    head = _preview(node.label or _short(node.session_id), limit=32)
    parts = [node.status, head]
    if node.stop_cause:
        parts.append(node.stop_cause)
    parts.append(f"run {node.run_id[:8]}" if node.run_id else "run none")
    if node.activity:
        parts.append(_preview(node.activity, limit=24))
    if node.has_queued_followup:
        parts.append("queued")
    if node.awaiting_approval:
        parts.append("approval")
    return indent + _preview(" · ".join(parts), limit=MAX_ROSTER_LABEL_CHARS)


def _non_negative(raw: object, field: str) -> int:
    if raw is None:
        return 0
    if type(raw) is not int or raw < 0:
        raise ValueError(f"{MONITOR_COMMAND} {field} must be a non-negative integer")
    return raw


def _bounded_limit(raw: object) -> int:
    if raw is None:
        return 25
    if type(raw) is not int or raw < 1 or raw > MAX_DETAIL_EVENTS:
        raise ValueError(f"{MONITOR_COMMAND} limit must be an integer within 1..{MAX_DETAIL_EVENTS}")
    return raw


def _roster(nodes: tuple[DescendantNode, ...]) -> CommandPresentation:
    if not nodes:
        return CommandPresentation(
            title="Running subagents (none)",
            view_type="choices",
            data={"options": [], "action": MONITOR_COMMAND, "argument_name": "target_session_id", "prepare_input": True},
            refresh_after_ms=MONITOR_REFRESH_MS,
        )
    by_parent: dict[str | None, list[DescendantNode]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_session_id, []).append(node)
    known = {node.session_id for node in nodes}
    ordered: list[tuple[DescendantNode, int]] = []

    def _visit(node: DescendantNode, level: int) -> None:
        ordered.append((node, level))
        for child in by_parent.get(node.session_id, []):
            _visit(child, level + 1)

    for node in nodes:
        if node.parent_session_id not in known:
            _visit(node, 0)
    return CommandPresentation(
        title="Running subagents",
        view_type="choices",
        data={
            "options": [{"id": node.session_id, "label": _node_label(node, indent="  " * level)} for node, level in ordered],
            "action": MONITOR_COMMAND,
            "argument_name": "target_session_id",
            "prepare_input": True,
        },
        refresh_after_ms=MONITOR_REFRESH_MS,
    )


def _detail_text(node: DescendantNode, parent: DescendantNode | None, history: str, *, truncated: bool) -> str:
    parent_name = _preview(parent.label or _short(parent.session_id), limit=64) if parent else "this session"
    activity = _preview(node.activity or "unknown", limit=96)
    lines = [
        f"{node.status} · {_preview(node.label or _short(node.session_id))}",
        f"session {node.session_id}",
        f"parent {parent_name}" + (f" ({parent.session_id})" if parent else ""),
        f"run {node.run_id or 'none'}" + (f" · stop {node.stop_cause}" if node.stop_cause else ""),
        f"turn {node.turn} · last event {node.last_event_sequence}",
        f"activity {activity}",
    ]
    if node.has_queued_followup:
        lines.append("queued follow-up waiting at the next safe boundary")
    if node.awaiting_approval:
        lines.append("parked waiting for approval (resume it from its own session)")
    lines.append("")
    lines.append(history or "(no readable events in this range)")
    if truncated:
        lines.append("")
        lines.append("More history: use Next page.")
    return "\n".join(lines)


def _detail(
    node: DescendantNode,
    parent: DescendantNode | None,
    *,
    after_sequence: int,
    history_offset: int,
    limit: int,
    page_text: str,
    next_after: int,
    next_offset: int,
    truncated: bool,
) -> CommandPresentation:
    base: dict[str, object] = {
        "target_session_id": node.session_id,
        "after_sequence": after_sequence,
        "history_offset": history_offset,
        "limit": limit,
    }
    actions: list[dict[str, object]] = [
        {"label": "Back to roster", "action": MONITOR_COMMAND, "arguments": {}, "prepare_input": True},
        {
            "label": "Cancel subtree",
            "action": MONITOR_CANCEL_COMMAND,
            "arguments": {"target_session_id": node.session_id, "expected_run_id": node.run_id},
        },
    ]
    if truncated:
        actions.append(
            {
                "label": "Next page",
                "action": MONITOR_COMMAND,
                "arguments": {
                    "target_session_id": node.session_id,
                    "after_sequence": next_after,
                    "history_offset": next_offset,
                    "limit": limit,
                },
                "prepare_input": True,
            }
        )
    actions.append({"label": "Refresh", "action": MONITOR_COMMAND, "arguments": dict(base), "prepare_input": True})
    return CommandPresentation(
        title=f"Subagent {_short(node.session_id)}",
        view_type="markdown",
        data={"content": _detail_text(node, parent, page_text, truncated=truncated), "actions": actions},
        refresh_after_ms=MONITOR_REFRESH_MS,
    )


def _render(arguments: Mapping[str, object], services: PluginServices) -> CommandPresentation:
    target = arguments.get("target_session_id")
    if target is None:
        return _roster(services.list_descendants())
    if not isinstance(target, str) or not target:
        raise ValueError(f"{MONITOR_COMMAND} target_session_id must be a non-empty string")
    nodes = {node.session_id: node for node in services.list_descendants()}
    node = nodes.get(target)
    if node is None:
        raise ValueError(f"{MONITOR_COMMAND} unknown subagent: {target}")
    after_sequence = _non_negative(arguments.get("after_sequence"), "after_sequence")
    history_offset = _non_negative(arguments.get("history_offset"), "history_offset")
    limit = _bounded_limit(arguments.get("limit"))
    page = services.read_session_history(target, after_sequence=after_sequence, offset=history_offset, limit=limit, page_bytes=DETAIL_PAGE_BYTES)
    return _detail(
        node,
        nodes.get(node.parent_session_id or ""),
        after_sequence=after_sequence,
        history_offset=history_offset,
        limit=limit,
        page_text=page.text,
        next_after=page.next_after_sequence,
        next_offset=page.next_offset,
        truncated=page.truncated,
    )


class MonitorCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name=MONITOR_COMMAND,
        description="Subagents",
        argument_schema={
            "type": "object",
            "properties": {
                "target_session_id": {"type": "string", "description": "One descendant session to inspect."},
                "after_sequence": {"type": "integer", "minimum": 0},
                "history_offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_DETAIL_EVENTS},
            },
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
        slash_visible=False,
        default_shortcut="alt+a",
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        return _render(invocation.arguments, services)

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        try:
            presentation = _render(invocation.arguments, services)
        except (ValueError, PermissionError, RuntimeError) as exc:
            return CommandResult(status="error", error=str(exc))
        return CommandResult(status="ok", output="Subagent monitor", presentation=presentation)


class MonitorCancelCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name=MONITOR_CANCEL_COMMAND,
        description="Subagent monitor",
        argument_schema={
            "type": "object",
            "properties": {
                "target_session_id": {"type": "string", "description": "Descendant subtree root to cancel."},
                "expected_run_id": {"type": ["string", "null"], "description": "Run the target must still carry (null if never started)."},
            },
            "required": ["target_session_id", "expected_run_id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
        slash_visible=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw_target = invocation.arguments.get("target_session_id")
        if not isinstance(raw_target, str) or not raw_target:
            return CommandResult(status="error", error=f"{MONITOR_CANCEL_COMMAND} requires a non-empty 'target_session_id'")
        if "expected_run_id" not in invocation.arguments:
            return CommandResult(status="error", error=f"{MONITOR_CANCEL_COMMAND} requires an explicit 'expected_run_id' (null if never started)")
        expected = invocation.arguments.get("expected_run_id")
        if expected is not None and (not isinstance(expected, str) or not expected):
            return CommandResult(status="error", error=f"{MONITOR_CANCEL_COMMAND} expected_run_id must be a non-empty string or null")
        try:
            nodes = {node.session_id: node for node in services.list_descendants()}
        except (ValueError, PermissionError, RuntimeError) as exc:
            return CommandResult(status="error", error=str(exc))
        node = nodes.get(raw_target)
        if node is None:
            return CommandResult(status="error", error=f"{MONITOR_CANCEL_COMMAND} unknown subagent: {raw_target} (not a descendant of this session)")
        if node.run_id != expected:
            return CommandResult(status="error", error=f"{MONITOR_CANCEL_COMMAND} target moved to a newer run; refresh and confirm again")
        descendants = [child for child in nodes.values() if _is_below(child, raw_target, nodes)]
        scope = [raw_target, *(child.session_id for child in descendants)]
        if invocation.context.actor != "user":
            request_id = uuid4().hex
            names = ", ".join(_short(item) for item in scope[:6])
            more = f" (+{len(scope) - 6} more)" if len(scope) > 6 else ""
            return CommandResult(
                status="pending_confirmation",
                confirmation=ConfirmationRequest(
                    request_id=request_id,
                    owner="subagents",
                    command_name=self.definition.name,
                    arguments={"target_session_id": raw_target, "expected_run_id": expected},
                    ui_type="confirmation",
                    ui_data={
                        "request_id": request_id,
                        "message": (
                            f"Cancel {_preview(node.label or _short(raw_target), limit=64)} and {len(descendants)} "
                            f"descendant(s) ({names}{more})? Root and siblings are unaffected."
                        ),
                        "actions": ["allow", "deny"],
                    },
                ),
            )
        try:
            receipt = services.cancel_descendant_subtree(raw_target, expected_run_id=expected, reason="subagent monitor cancellation")
        except (ValueError, PermissionError, RuntimeError) as exc:
            return CommandResult(status="error", error=str(exc))
        accepted = len(receipt.cancelled) + len(receipt.disarmed_queued) + len(receipt.claimed_reserved)
        newer = f"; {len(receipt.observed_newer_run)} moved to a newer run and were left running" if receipt.observed_newer_run else ""
        if accepted:
            output = (
                f"Cancellation requested for {accepted} session(s) under {_short(raw_target)} "
                f"({len(receipt.already_terminal)} already terminal{newer}). Watch the roster for the settled state."
            )
        else:
            output = f"No live work under {_short(raw_target)} ({len(receipt.already_terminal)} already terminal{newer})."
        return CommandResult(status="ok", output=output, data=receipt.as_payload())


def _is_below(node: DescendantNode, root: str, nodes: Mapping[str, DescendantNode]) -> bool:
    current = node.parent_session_id
    seen = {node.session_id}
    while current is not None and current not in seen:
        if current == root:
            return True
        seen.add(current)
        parent = nodes.get(current)
        current = None if parent is None else parent.parent_session_id
    return False


__all__ = ["MONITOR_CANCEL_COMMAND", "MONITOR_COMMAND", "MonitorCancelCommand", "MonitorCommand"]
