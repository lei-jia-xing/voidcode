from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import Input, OptionList, Tree
from textual.widgets.tree import TreeNode

from voidcode.tui.app import VoidCodeTUI


def _tool_children(tree: Tree[Any]) -> list[TreeNode[Any]]:
    return list(cast(Any, tree.root.children))


@pytest.mark.anyio
async def test_tui_mvp_smoke_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = VoidCodeTUI(workspace=workspace, approval_mode="ask")

    async with app.run_test() as pilot:
        # 1. Submit request that requires approval
        composer = app.query_one("#composer-input", Input)
        composer.value = "write hello.txt hi"
        await pilot.press("enter")

        # Wait for the stream to reach "Waiting approval"
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert app.current_state == "Waiting approval"

        # Check that tool tree shows waiting
        tree = cast(Tree[Any], app.query_one("#tool-activity-tree", Tree))
        # Should have a node that says Waiting Approval
        tool_children = _tool_children(tree)
        assert len(tool_children) == 1
        assert "Waiting Approval" in str(tool_children[0].label)

        # 2. Handle approval
        app.screen.dismiss("allow")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert app.current_state == "Completed"
        assert "Completed" in str(tool_children[0].label)

        # Check file was written
        assert (workspace / "hello.txt").read_text() == "hi"

        # 3. List and reopen
        await pilot.click("#new-session-btn")
        await pilot.pause()
        assert app.current_state == "Idle"

        session_list = app.query_one("#session-list", OptionList)
        assert session_list.option_count == 1

        # Re-select the session
        session_list.highlighted = 0
        session_list.action_select()
        await pilot.pause()
        await pilot.pause()

        assert app.current_state == "Completed"

        # Verify the tree is populated from the resumed events
        tree2 = cast(Tree[Any], app.query_one("#tool-activity-tree", Tree))
        tool_children_after_replay = _tool_children(tree2)
        assert len(tool_children_after_replay) == 1
        assert "Completed" in str(tool_children_after_replay[0].label)


@pytest.mark.anyio
async def test_tui_mvp_reopen_waiting_session(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    app = VoidCodeTUI(workspace=workspace, approval_mode="ask")
    # Run first pass just to get to waiting
    async with app.run_test() as pilot:
        composer = app.query_one("#composer-input", Input)
        composer.value = "write wait.txt hi"
        await pilot.press("enter")

        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert app.current_state == "Waiting approval"
        pass

    # Restart app to simulate reopening
    app2 = VoidCodeTUI(workspace=workspace, approval_mode="ask")
    async with app2.run_test() as pilot2:
        await pilot2.pause()

        session_list = app2.query_one("#session-list", OptionList)
        assert session_list.option_count == 1

        # Select the session
        session_list.highlighted = 0
        session_list.action_select()
        await pilot2.pause()
        await pilot2.pause()

        assert app2.current_state == "Waiting approval"

        # Now approve
        app2.screen.dismiss("allow")
        await pilot2.pause()
        await pilot2.pause()
        await pilot2.pause()

        assert app2.current_state == "Completed"

        # Verify file
        assert (workspace / "wait.txt").read_text() == "hi"
