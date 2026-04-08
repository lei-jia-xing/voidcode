from pathlib import Path
from threading import Event
from unittest.mock import MagicMock

import pytest
from textual.widgets import TextArea

from voidcode.tui.app import ConversationScreen, StartupScreen, TuiBootstrap, VoidCodeTuiApp
from voidcode.tui.widgets.prompt_bar import PromptBar
from voidcode.tui.widgets.session_view import SessionView


@pytest.mark.anyio
async def test_tui_layout_resize_preserves_focus() -> None:
    bootstrap = TuiBootstrap(workspace=Path("/tmp"), session_id=None)
    app = VoidCodeTuiApp(bootstrap)
    async with app.run_test(size=(80, 24)) as pilot:
        # Start with prompt input focus
        assert isinstance(app.focused, TextArea)

        await pilot.resize_terminal(40, 12)
        await pilot.pause()

        # Focus is preserved
        assert isinstance(app.focused, TextArea)


@pytest.mark.anyio
async def test_tui_layout_narrow_terminal_boot_keeps_startup_prompt_focus() -> None:
    bootstrap = TuiBootstrap(workspace=Path("/tmp"), session_id=None)
    app = VoidCodeTuiApp(bootstrap)

    async with app.run_test(size=(32, 8)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, StartupScreen)
        assert isinstance(app.focused, TextArea)

        await pilot.resize_terminal(28, 7)
        await pilot.pause()

        startup_view = app.get_active_startup_view()
        assert startup_view is not None
        assert isinstance(app.screen, StartupScreen)
        assert isinstance(app.focused, TextArea)


@pytest.mark.anyio
async def test_tui_layout_startup_submit_transitions_to_conversation_with_prompt_intact() -> None:
    release_stream = Event()
    runtime_client = MagicMock()
    runtime_client.list_sessions.return_value = ()
    runtime_client.open_session.side_effect = AssertionError("open_session should not be used")
    runtime_client.resolve_approval.return_value = iter(())

    def stream_run(prompt: str, **_: object):
        assert prompt == "read README.md"
        release_stream.wait(timeout=2)
        return iter(())

    runtime_client.stream_run.side_effect = stream_run

    app = VoidCodeTuiApp(
        TuiBootstrap(workspace=Path("/tmp"), session_id=None),
        runtime_client=runtime_client,
    )

    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.press(*list("read README.md"))
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ConversationScreen)

        conversation_screen = app.screen
        conversation_view = conversation_screen.query_one(SessionView)
        prompt_bar = conversation_view.query_one(PromptBar)
        assert conversation_view.display_state.status == "running"
        assert conversation_view.prompt_draft == "read README.md"
        assert prompt_bar.submit_disabled is True
        assert prompt_bar.status_text == "Busy · run in progress"
        runtime_client.stream_run.assert_called_once_with(
            "read README.md",
            session_id=None,
            metadata={"client": "tui"},
            allocate_session_id=True,
        )

        release_stream.set()
        for worker in app.workers:
            await worker.wait()
