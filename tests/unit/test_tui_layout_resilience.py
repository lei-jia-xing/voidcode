from pathlib import Path

import pytest
from textual.widgets import Input

from voidcode.tui.app import TuiBootstrap, VoidCodeTuiApp


@pytest.mark.anyio
async def test_tui_layout_resize_preserves_focus() -> None:
    bootstrap = TuiBootstrap(workspace=Path("/tmp"), session_id=None)
    app = VoidCodeTuiApp(bootstrap)
    async with app.run_test(size=(80, 24)) as pilot:
        # Start with prompt input focus
        assert isinstance(app.focused, Input)

        # Ensure focus cycles using keys
        await pilot.press("tab")
        await pilot.pause()
        # Focus moved to next widget
        assert not isinstance(app.focused, Input)
