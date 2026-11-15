"""Public monitor navigation preserves data within the SDK view payload budget."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from jsonschema import validate

from voidcode.plugins.subagents.monitor import MONITOR_COMMAND, MonitorCommand
from voidcode.sdk.commands import CommandInvocation, CommandPresentation
from voidcode.sdk.history import HistoryPage
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import DescendantNode, PluginServices
from voidcode.sdk.views import MAX_VIEW_DATA_BYTES, ChoicesViewData, MarkdownViewData, choices_dispatch


class MonitorServices(PluginServices):
    def __init__(self, nodes: tuple[DescendantNode, ...]) -> None:
        self.nodes = nodes
        self.history_reads: list[str] = []

    def list_descendants(self) -> tuple[DescendantNode, ...]:
        return self.nodes

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
        self.history_reads.append(session_id)
        return HistoryPage("## history-marker\ncomplete history", 1, 0, False)


def _open(tmp_path: Path, services: MonitorServices, arguments: Mapping[str, object], *, invoke: bool) -> CommandPresentation:
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


@pytest.mark.parametrize("invoke", [False, True])
def test_roster_pagination_reaches_every_full_label_and_detail(tmp_path: Path, invoke: bool) -> None:
    nodes = tuple(
        DescendantNode(
            session_id=f"child-{index}",
            parent_session_id=None,
            depth=1,
            status="running",
            run_id=f"run-{index}",
            label=f"label-{index}-" + "查询目标" * 160,
            activity=f"activity-{index}-" + "progress " * 80,
        )
        for index in range(64)
    )
    assert len(json.dumps([node.as_payload() for node in nodes]).encode()) > MAX_VIEW_DATA_BYTES
    services = MonitorServices(nodes)
    arguments: Mapping[str, object] = {}
    visited = 0
    pages = 0
    cursors: set[str] = set()
    while True:
        roster = _open(tmp_path, services, arguments, invoke=invoke)
        view = ChoicesViewData.from_payload(roster.data)
        pages += 1
        following = None
        for option in view.options:
            if option.label == "Back to first roster page":
                continue
            action, selected = choices_dispatch(view, option.id)
            assert action == MONITOR_COMMAND
            if option.label == "Next roster page":
                following = selected
                continue
            node = nodes[visited]
            assert node.label in option.label
            assert node.activity in option.label
            detail = MarkdownViewData.from_payload(_open(tmp_path, services, selected, invoke=invoke).data)
            assert node.label in detail.content
            assert node.activity in detail.content
            assert f"session {node.session_id}\n" in detail.content
            cancel = next(action for action in detail.actions if action.label == "Cancel subtree")
            assert dict(cancel.arguments) == {"target_session_id": node.session_id, "expected_run_id": node.run_id}
            back = next(action for action in detail.actions if action.label == "Back to roster")
            restored = _open(tmp_path, services, back.arguments, invoke=invoke)
            assert ChoicesViewData.from_payload(restored.data) == view
            visited += 1
        if following is None:
            break
        cursor = json.dumps(following)
        assert cursor not in cursors, "roster navigation must advance"
        cursors.add(cursor)
        arguments = following
    assert visited == len(nodes)
    assert pages > 1
    assert len(services.history_reads) == len(nodes)


@pytest.mark.parametrize("invoke", [False, True])
def test_single_oversized_label_and_parent_activity_are_readable_via_detail_pages(tmp_path: Path, invoke: bool) -> None:
    parent = DescendantNode(
        session_id="parent-exact-id",
        parent_session_id=None,
        depth=1,
        status="running",
        label="parent-start:" + '父🚀\n"\\\t' * 5000 + ":parent-end",
    )
    node = DescendantNode(
        session_id="child-exact-id",
        parent_session_id=parent.session_id,
        depth=2,
        status="waiting",
        run_id="run-exact-id",
        label="child-start:" + '子🚀\n"\\\t' * 5000 + ":child-end",
        activity="activity-start:" + '忙碌🚀\n"\\\t' * 5000 + ":activity-end",
        awaiting_approval=True,
    )
    services = MonitorServices((parent, node))
    roster = ChoicesViewData.from_payload(_open(tmp_path, services, {}, invoke=invoke).data)
    first = next(option for option in roster.options if option.label == "Next roster page")
    _, arguments = choices_dispatch(roster, first.id)
    roster = ChoicesViewData.from_payload(_open(tmp_path, services, arguments, invoke=invoke).data)
    child = next(option for option in roster.options if "child-start:" in option.label)
    assert "[open full details]" in child.label
    _, arguments = choices_dispatch(roster, child.id)
    pieces: list[str] = []
    cursors: set[str] = set()
    while True:
        detail = MarkdownViewData.from_payload(_open(tmp_path, services, arguments, invoke=invoke).data)
        pieces.append(detail.content)
        cancel = next(action for action in detail.actions if action.label == "Cancel subtree")
        assert dict(cancel.arguments) == {"target_session_id": node.session_id, "expected_run_id": node.run_id}
        refresh = next(action for action in detail.actions if action.label == "Refresh")
        assert MarkdownViewData.from_payload(_open(tmp_path, services, refresh.arguments, invoke=invoke).data) == detail
        following = next((action for action in detail.actions if action.label == "Next page"), None)
        if following is None:
            break
        cursor = json.dumps(dict(following.arguments))
        assert cursor not in cursors, "detail navigation must advance"
        cursors.add(cursor)
        arguments = following.arguments
    full = "".join(pieces)
    assert parent.label in full
    assert node.label in full
    assert node.activity in full
    assert f"session {node.session_id}\n" in full
    assert full.count("history-marker") == 1
    assert len(pieces) > 1
    back = next(action for action in detail.actions if action.label == "Back to roster")
    assert ChoicesViewData.from_payload(_open(tmp_path, services, back.arguments, invoke=invoke).data) == roster
