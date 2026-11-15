"""Running-subagent monitor: a read-only roster plus detail views over core-owned descendants.

The roster and every detail page render through read-only ``command.input``
preparation only; only the explicit subtree cancellation goes through a
governed, confirmed command invocation.
"""

from __future__ import annotations

import json
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
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES

MONITOR_COMMAND = "subagents.monitor"
MONITOR_CANCEL_COMMAND = "subagents.monitor.cancel"
MONITOR_REFRESH_MS = 1000
DETAIL_PAGE_BYTES = 8 * 1024
MAX_DETAIL_EVENTS = 50


def _short(session_id: str) -> str:
    return session_id[:8]


def _clean_text(text: str) -> str:
    return " ".join(text.split())


def _node_label(node: DescendantNode, *, indent: str = "") -> str:
    parts = [node.status, node.label or node.session_id]
    if node.stop_cause:
        parts.append(node.stop_cause)
    parts.append(f"run {node.run_id}" if node.run_id else "run none")
    if node.activity:
        parts.append(node.activity)
    if node.has_queued_followup:
        parts.append("queued")
    if node.awaiting_approval:
        parts.append("approval")
    return indent + " · ".join(parts)


def _view_bytes(data: Mapping[str, object]) -> int:
    # Match validate_view_data's JSON escaping, not just UTF-8 source length.
    return len(json.dumps(data, allow_nan=False).encode("utf-8"))


def _choice_id(**arguments: object) -> str:
    return json.dumps(arguments, ensure_ascii=False)


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


def _roster(nodes: tuple[DescendantNode, ...], *, offset: int = 0) -> CommandPresentation:
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

    options: list[dict[str, object]] = []
    if offset:
        options.append({"id": _choice_id(roster_offset=0), "label": "Back to first roster page"})

    def _data(rows: list[dict[str, object]]) -> dict[str, object]:
        return {"options": rows, "action": MONITOR_COMMAND, "argument_name": "selection", "prepare_input": True, "style": None}

    def _next(index: int) -> list[dict[str, object]]:
        return [{"id": _choice_id(roster_offset=index), "label": "Next roster page"}] if index < len(ordered) else []

    for index in range(offset, len(ordered)):
        node, level = ordered[index]
        row: dict[str, object] = {
            "id": _choice_id(target_session_id=node.session_id, roster_offset=offset),
            "label": _node_label(node, indent="  " * level),
        }
        following = _next(index + 1)
        if _view_bytes(_data([*options, row, *following])) > MAX_VIEW_DATA_BYTES:
            if index != offset:
                break
            # Only an individually oversized row is folded. Full, unchanged fields
            # remain available in the detail pages reached by this exact selector.
            label = str(row["label"])
            low, high = 0, len(label)
            while low < high:
                end = (low + high + 1) // 2
                row["label"] = label[:end] + "… [open full details]"
                if _view_bytes(_data([*options, row, *following])) <= MAX_VIEW_DATA_BYTES:
                    low = end
                else:
                    high = end - 1
            row["label"] = label[:low] + "… [open full details]"
            if _view_bytes(_data([*options, row, *following])) > MAX_VIEW_DATA_BYTES:
                raise ValueError("monitor navigation exceeds the view-data budget")
        options.append(row)
    else:
        index = len(ordered)

    # On a budget break, index names the first undisplayed descendant.
    options.extend(_next(index))
    return CommandPresentation(
        title="Running subagents" if nodes else "Running subagents (none)",
        view_type="choices",
        data=_data(options),
        refresh_after_ms=MONITOR_REFRESH_MS,
    )


def _detail_text(node: DescendantNode, parent: DescendantNode | None, history: str, *, truncated: bool) -> str:
    parent_name = parent.label or parent.session_id if parent else "this session"
    lines = [
        f"{node.status} · {node.label or node.session_id}",
        f"session {node.session_id}",
        f"parent {parent_name}" + (f" ({parent.session_id})" if parent else ""),
        f"run {node.run_id or 'none'}" + (f" · stop {node.stop_cause}" if node.stop_cause else ""),
        f"turn {node.turn} · last event {node.last_event_sequence}",
        f"activity {node.activity or 'unknown'}",
    ]
    if node.has_queued_followup:
        lines.append("queued follow-up waiting at the next safe boundary")
    if node.awaiting_approval:
        lines.append("parked waiting for approval (resume it from its own session)")
    lines.extend(("", history or "(no readable events in this range)"))
    if truncated:
        lines.extend(("", "More history: use Next page."))
    return "\n".join(lines)


def _detail(
    node: DescendantNode,
    parent: DescendantNode | None,
    *,
    roster_offset: int = 0,
    detail_offset: int = 0,
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
        "roster_offset": roster_offset,
    }
    text = _detail_text(node, parent, page_text, truncated=truncated)

    def _data(end: int) -> dict[str, object]:
        actions: list[dict[str, object]] = [
            {"label": "Back to roster", "action": MONITOR_COMMAND, "arguments": {"roster_offset": roster_offset}, "prepare_input": True},
            {
                "label": "Cancel subtree",
                "action": MONITOR_CANCEL_COMMAND,
                "arguments": {"target_session_id": node.session_id, "expected_run_id": node.run_id},
                "prepare_input": False,
            },
        ]
        if end < len(text) or truncated:
            following = {**base, "detail_offset": end} if end < len(text) else {**base, "after_sequence": next_after, "history_offset": next_offset}
            actions.append({"label": "Next page", "action": MONITOR_COMMAND, "arguments": following, "prepare_input": True})
        actions.append({"label": "Refresh", "action": MONITOR_COMMAND, "arguments": {**base, "detail_offset": detail_offset}, "prepare_input": True})
        return {"content": text[detail_offset:end], "actions": actions, "style": None}

    end = len(text)
    if _view_bytes(_data(end)) > MAX_VIEW_DATA_BYTES:
        low, high = detail_offset, len(text)
        while low < high:
            candidate = (low + high + 1) // 2
            if _view_bytes(_data(candidate)) <= MAX_VIEW_DATA_BYTES:
                low = candidate
            else:
                high = candidate - 1
        end = low
        if end == detail_offset:
            raise ValueError("monitor navigation exceeds the view-data budget")
    return CommandPresentation(
        title=f"Subagent {_short(node.session_id)}",
        view_type="markdown",
        data=_data(end),
        refresh_after_ms=MONITOR_REFRESH_MS,
    )


def _render(arguments: Mapping[str, object], services: PluginServices) -> CommandPresentation:
    selection = arguments.get("selection")
    if selection is not None:
        if not isinstance(selection, str):
            raise ValueError("monitor selection must be a string")
        selected = json.loads(selection)
        if not isinstance(selected, dict) or set(selected) - {"target_session_id", "roster_offset"}:
            raise ValueError("invalid monitor selection")
        arguments = {**arguments, **selected}
    target = arguments.get("target_session_id")
    roster_offset = _non_negative(arguments.get("roster_offset"), "roster_offset")
    if target is None:
        return _roster(services.list_descendants(), offset=roster_offset)
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
        roster_offset=roster_offset,
        after_sequence=after_sequence,
        history_offset=history_offset,
        detail_offset=_non_negative(arguments.get("detail_offset"), "detail_offset"),
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
                "roster_offset": {"type": "integer", "minimum": 0},
                "detail_offset": {"type": "integer", "minimum": 0, "description": "Character cursor within the current detail/history page."},
                "selection": {"type": "string", "description": "Opaque roster navigation selection."},
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
                            f"Cancel {_clean_text(node.label) if node.label else _short(raw_target)} and {len(descendants)} "
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
