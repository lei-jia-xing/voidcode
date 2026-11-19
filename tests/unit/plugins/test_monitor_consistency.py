"""Detail paging detects concurrent descendant changes instead of paging shifted text."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from jsonschema import validate

from voidcode.core.history_renderer import HistoryPage
from voidcode.plugins.subagents.monitor import MONITOR_COMMAND, MonitorCommand
from voidcode.sdk.commands import CommandInvocation, CommandPresentation
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import DescendantNode, PluginServices
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES, MarkdownViewData


class MutableMonitorServices(PluginServices):
    def __init__(self, nodes: tuple[DescendantNode, ...], pages: Mapping[tuple[int, int], HistoryPage]) -> None:
        self.nodes = list(nodes)
        self.pages = dict(pages)

    def list_descendants(self) -> tuple[DescendantNode, ...]:
        return tuple(self.nodes)

    def read_session_history(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        offset: int = 0,
        limit: int = 100,
        page_bytes: int = 40960,
    ) -> HistoryPage:
        assert session_id in {node.session_id for node in self.nodes}
        return self.pages[(after_sequence, offset)]

    def replace(self, node: DescendantNode) -> None:
        for index, current in enumerate(self.nodes):
            if current.session_id == node.session_id:
                self.nodes[index] = node
                return
        raise AssertionError(f"unknown descendant: {node.session_id}")


def _open(tmp_path: Path, services: MutableMonitorServices, arguments: Mapping[str, object], *, invoke: bool) -> CommandPresentation:
    command = MonitorCommand()
    validate(json_wire_object(arguments), json_wire_object(command.definition.argument_schema))
    context = ToolContext(workspace=tmp_path, session_id="root", run_id=None, invocation_id="monitor", revision_id="revision", actor="user")
    call = CommandInvocation(context=context, name=MONITOR_COMMAND, arguments=arguments)
    if invoke:
        result = command.invoke(call, services)
        assert result.status == "ok", result.error
        assert result.presentation is not None
        presentation = result.presentation
    else:
        presentation = command.prepare_input(call, services)
    assert len(json.dumps(json_wire_object(presentation.data), allow_nan=False).encode("utf-8")) <= MAX_VIEW_DATA_BYTES
    return presentation


def _detail(tmp_path: Path, services: MutableMonitorServices, arguments: Mapping[str, object], *, invoke: bool) -> MarkdownViewData:
    return MarkdownViewData.from_payload(_open(tmp_path, services, arguments, invoke=invoke).data)


def _next(view: MarkdownViewData) -> Mapping[str, object]:
    return next(action for action in view.actions if action.label == "Next page").arguments


def _node() -> DescendantNode:
    return DescendantNode(
        session_id="child-consistency-id",
        parent_session_id=None,
        depth=1,
        status="running",
        run_id="run-before",
        label="consistency child",
        activity="working step one",
        turn=3,
        last_event_sequence=9,
    )


@pytest.mark.parametrize("invoke", [False, True])
def test_detail_page_turn_after_state_change_returns_stale_notice(tmp_path: Path, invoke: bool) -> None:
    node = _node()
    history = "".join(f"history line {index:05d}\n" for index in range(6000))
    services = MutableMonitorServices((node,), {(0, 0): HistoryPage(history, 1, 0, False)})
    first = _detail(tmp_path, services, {"target_session_id": node.session_id}, invoke=invoke)
    cursor = dict(_next(first))
    assert cursor["detail_offset"] > 0

    services.replace(dataclasses.replace(node, status="waiting", activity="working step two"))
    stale = _detail(tmp_path, services, cursor, invoke=invoke)
    assert stale.content.startswith(f"Subagent {node.session_id[:8]} changed since this page was rendered")
    assert "waiting" in stale.content
    assert "Re-open the detail view" in stale.content
    assert "history line 00000" not in stale.content
    assert [action.label for action in stale.actions] == ["Back to roster", "Re-open detail"]

    reopen = next(action for action in stale.actions if action.label == "Re-open detail").arguments
    assert "detail_offset" not in reopen
    assert "detail_version" not in reopen
    fresh = _detail(tmp_path, services, reopen, invoke=invoke)
    assert "waiting" in fresh.content
    assert "working step two" in fresh.content
    assert _next(fresh)["detail_offset"] > 0


@pytest.mark.parametrize("invoke", [False, True])
def test_detail_paging_without_changes_reaches_full_text(tmp_path: Path, invoke: bool) -> None:
    node = _node()
    history = "".join(f"history line {index:05d}\n" for index in range(6000))
    services = MutableMonitorServices((node,), {(0, 0): HistoryPage(history, 1, 0, False)})
    arguments: Mapping[str, object] = {"target_session_id": node.session_id}
    pieces: list[str] = []
    versions: set[str] = set()
    while True:
        presentation = _open(tmp_path, services, arguments, invoke=invoke)
        assert "(stale)" not in (presentation.title or "")
        view = MarkdownViewData.from_payload(presentation.data)
        pieces.append(view.content)
        refresh = next(action for action in view.actions if action.label == "Refresh").arguments
        versions.add(str(refresh["detail_version"]))
        assert _detail(tmp_path, services, refresh, invoke=invoke) == view
        following = next((action for action in view.actions if action.label == "Next page"), None)
        if following is None:
            break
        versions.add(str(following.arguments["detail_version"]))
        arguments = following.arguments
    assert len(pieces) > 1
    assert len(versions) == 1
    full = "".join(pieces)
    assert "running" in full
    assert "working step one" in full
    assert history in full


def test_detail_version_rides_same_window_cursors_only(tmp_path: Path) -> None:
    node = _node()
    history = "".join(f"history line {index:05d}\n" for index in range(6000))
    services = MutableMonitorServices((node,), {(0, 0): HistoryPage(history, 1, 0, False)})
    first = _detail(tmp_path, services, {"target_session_id": node.session_id}, invoke=False)
    cursor = dict(_next(first))
    assert isinstance(cursor.get("detail_version"), str) and cursor["detail_version"]
    refresh = next(action for action in first.actions if action.label == "Refresh").arguments
    assert refresh["detail_version"] == cursor["detail_version"]

    tampered = {**cursor, "detail_version": "bogus-version"}
    stale = _detail(tmp_path, services, tampered, invoke=False)
    assert "changed since this page was rendered" in stale.content
    assert "Next page" not in [action.label for action in stale.actions]


def test_history_window_advance_carries_no_stale_version(tmp_path: Path) -> None:
    node = _node()
    services = MutableMonitorServices(
        (node,),
        {
            (0, 0): HistoryPage("window one", 7, 0, True),
            (7, 0): HistoryPage("window two", 9, 0, False),
        },
    )
    first = _detail(tmp_path, services, {"target_session_id": node.session_id}, invoke=False)
    cursor = dict(_next(first))
    assert cursor["after_sequence"] == 7
    assert "Detail_offset" not in cursor
    assert "detail_version" not in cursor
    second = _detail(tmp_path, services, cursor, invoke=False)
    assert "window two" in second.content
    assert "Next page" not in [action.label for action in second.actions]
