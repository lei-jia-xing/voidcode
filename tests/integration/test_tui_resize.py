"""Real-terminal (PTY) acceptance for resize, reflow, scrollback, and caret re-anchoring.

Every scenario drives ``python -m voidcode`` in a real POSIX pseudo-terminal and
interprets the writer's ANSI output with :mod:`tests.fixtures.tui_pty`. Width
changes clamp future rows without reflowing history, so committed rows are
counted in the whole document: a line lost to a misplaced erase or duplicated by
a stale live region is observable, while terminal-specific reflow is not assumed.

Composer image ingress is covered at the path-entry/badge layer: the badge and
the draft must survive resizes together. Native Kitty pixel placement is not
observable through a plain PTY and already has a dedicated PTY resize check in
``test_artifact_tui_surface.py``.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.png import rgb_png
from tests.fixtures.tui_pty import AnsiScreen, TuiPty
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import bundled_sources, open_core

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX PTY support")

_RULE = "─"
_STREAM_WORDS = tuple(f"STREAM-TOKEN-{index:03d}" for index in range(80))
_STREAM_CONTENT = " ".join(_STREAM_WORDS) + " STREAM-COMPLETE-MARKER"


@contextmanager
def _loopback(replies: list[dict[str, object]], *, delay: float = 0.0, chunk_chars: int = 0) -> Generator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    server.stream_delay = delay
    server.stream_chunk_chars = chunk_chars
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _write_config(
    workspace: Path,
    *,
    server: OpenAILoopbackServer | None = None,
    approval_mode: str = "yolo",
    enabled: tuple[str, ...] = (),
) -> None:
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    for name in enabled:
        plugins[name] = {"enabled": True}
    config: dict[str, object] = {"schema_version": 1, "plugins": plugins}
    if server is not None:
        plugins["providers"] = {"enabled": True}
        config["model"] = {"model": "endpoint/exact-model"}
        config["policy"] = {"approval_mode": approval_mode}
        config["providers"] = {"endpoint": {"api_key": "loopback-secret", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}}
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _scene(
    tmp_path: Path,
    *,
    server: OpenAILoopbackServer | None = None,
    approval_mode: str = "yolo",
    enabled: tuple[str, ...] = (),
    name: str = "resize-scene",
) -> tuple[Path, Path, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    _write_config(workspace, server=server, approval_mode=approval_mode, enabled=enabled)
    with open_core(workspace, state_dir=state) as core:
        session_id = core.create_session(name).session_id
    return workspace, state, session_id


def _lines(screen: AnsiScreen) -> list[str]:
    return screen.visible().splitlines()


def _has_rule(width: int) -> Callable[[AnsiScreen], bool]:
    # The PTY peer answers no DECRQM queries, so the writer reserves the last
    # cell as an autowrap-safe fallback: an N-cell terminal renders N-1 cells.
    return lambda screen: any(line == _RULE * (width - 1) for line in _lines(screen))


def _assert_cursor_on(term: TuiPty, line_text: str, offset: int) -> None:
    """The native caret must sit ``offset`` characters into the rendered draft row."""
    for index, line in enumerate(_lines(term.screen)):
        column = line.find(line_text)
        if column >= 0:
            expected = (index, column + offset)
            assert term.screen.cursor_cell == expected, (
                f"caret {term.screen.cursor_cell} != {expected} for {line_text!r}+{offset}\n{term.screen.visible()}"
            )
            return
    raise AssertionError(f"draft row {line_text!r} not on screen:\n{term.screen.visible()}")


def test_resize_during_live_stream_reanchors_and_commits_each_row_once(tmp_path: Path) -> None:
    """Three resizes while tokens stream: no lost/duplicated live or committed rows."""
    reply = openai_response(message={"role": "assistant", "content": _STREAM_CONTENT, "tool_calls": []}, finish="stop")
    with _loopback([reply], delay=0.08, chunk_chars=48) as server:
        workspace, state, session_id = _scene(tmp_path, server=server, name="resize-stream")
        term = TuiPty(workspace, state, width=80, height=24, session_id=session_id)
        try:
            term.until("exact-mode")
            term.send(b"go streaming\r")
            term.until("STREAM-TOKEN-000", timeout=30)
            assert "working" in term.screen.visible(), term.screen.visible()

            for width, height in ((37, 13), (110, 29), (52, 9)):
                assert "STREAM-COMPLETE-MARKER" not in term.screen.document, "stream finished before its resize window"
                pending = next(word for word in _STREAM_WORDS if word not in term.screen.document)
                term.resize(width, height)
                term.until_screen(_has_rule(width), f"{width}-cell composer rule after resize", timeout=8)
                term.until(pending, timeout=20)
                visible = term.screen.visible()
                assert "working" in visible, f"stream did not stay live after resize to {width}x{height}:\n{visible}"
                assert "STREAM-COMPLETE-MARKER" not in term.screen.document, "stream finished before the resize sequence drained"

            term.until_gone("working", timeout=30)
            term.settle(0.6)
            document = term.screen.document
            assert document.count("go streaming") == 1, f"submitted prompt duplicated or lost:\n{document}"
            assert document.count("STREAM-COMPLETE-MARKER") == 1, f"final answer duplicated or lost:\n{document}"
            counts = [(word, document.count(word)) for word in _STREAM_WORDS]
            assert all(count == 1 for _word, count in counts), [item for item in counts if item[1] != 1]
            positions = [document.index(word) for word in _STREAM_WORDS]
            assert positions == sorted(positions), "committed answer lost token order"
            # The answer was committed after the last resize, so every committed
            # row (prompt through the composer rule) fits the final 52-cell width.
            lines = term.screen.lines
            prompt_row = next(index for index, line in enumerate(lines) if line.strip() == "go streaming")
            rule_row = next(index for index in range(prompt_row + 1, len(lines)) if lines[index] == _RULE * 51)
            overflow = [(index, line) for index, line in enumerate(lines[prompt_row:rule_row]) if len(line) > 52]
            assert not overflow, f"committed rows exceed the final width: {overflow}"
        finally:
            term.close()


def test_resize_preserves_multiline_draft_and_caret_across_reflows(tmp_path: Path) -> None:
    """A multiline draft with its caret mid-line survives shrink, grow, and submit."""
    workspace, state, session_id = _scene(tmp_path, name="resize-draft")
    term = TuiPty(workspace, state, width=80, height=24, session_id=session_id)
    try:
        term.until("no model")
        term.send(b"alpha-ONE\x1b\rbravo-TWO target\x1b\rcharlie-THREE")
        term.settle(0.5)
        for line in ("alpha-ONE", "bravo-TWO target", "charlie-THREE"):
            assert line in term.screen.visible(), f"multiline draft missing {line!r}:\n{term.screen.visible()}"
        # Up to the middle line, then line start plus six characters: the caret
        # sits between "bravo-" and "TWO target".
        term.send(b"\x1b[A\x01" + b"\x1b[C" * 6)
        term.settle(0.3)
        _assert_cursor_on(term, "bravo-TWO target", 6)

        inserted = ""
        for width, height, digit in ((37, 12, "1"), (120, 26, "2"), (44, 11, "3")):
            term.resize(width, height)
            term.until_screen(_has_rule(width), f"{width}-cell composer rule after resize", timeout=8)
            expected = f"bravo-{inserted}TWO target"
            assert expected in term.screen.visible(), f"draft not re-rendered at {width}x{height}:\n{term.screen.visible()}"
            _assert_cursor_on(term, expected, 6 + len(inserted))
            term.send(digit.encode())
            term.settle(0.2)
            inserted += digit
            updated = f"bravo-{inserted}TWO target"
            assert updated in term.screen.visible(), f"typing did not land at the caret after {width}x{height}:\n{term.screen.visible()}"
            _assert_cursor_on(term, updated, 6 + len(inserted))

        expected = f"bravo-{inserted}TWO target"
        term.send(b"\r")
        term.until("Error: no model configured or registered", timeout=15)
        term.settle(0.4)
        document = term.screen.document
        assert document.count(expected) == 1, f"submitted multiline draft duplicated or lost:\n{document}"
        assert "charlie-THREE" in document
        # The composer is usable again after the reflowed submit.
        term.send(b"after-submit")
        term.settle(0.3)
        assert "after-submit" in term.screen.visible(), term.screen.visible()
    finally:
        term.close()


def test_resize_preserves_image_path_entry_badge_and_draft(tmp_path: Path) -> None:
    """A typed image path and then its composer badge survive every resize with the draft."""
    workspace, state, session_id = _scene(tmp_path, name="resize-image")
    image = workspace / "badge.png"
    image.write_bytes(rgb_png(8, 8))
    term = TuiPty(workspace, state, width=80, height=24, session_id=session_id)
    try:
        term.until("no model")
        term.send(b"describe")
        term.settle(0.3)
        _assert_cursor_on(term, "describe", 8)

        term.send(b"\x1bi")  # Alt+I opens the path row without touching the draft
        term.until("Image path:", timeout=10)
        term.send(str(image).encode())
        term.settle(0.2)
        for width, height in ((37, 12), (110, 28), (46, 11)):
            term.resize(width, height)
            term.until_screen(_has_rule(width), f"{width}-cell composer rule after resize", timeout=8)
            visible = term.screen.visible()
            lines = visible.splitlines()
            assert "Image path:" in visible, f"image path row lost at {width}x{height}:\n{visible}"
            assert "describe" in visible, f"draft lost while entering an image path at {width}x{height}:\n{visible}"
            assert term.screen.cursor_cell[0] == next(index for index, line in enumerate(lines) if "Image path:" in line), (
                f"caret left the image path row at {width}x{height}:\n{visible}"
            )

        term.send(b"\r")
        term.until("image/png", timeout=15)
        _assert_cursor_on(term, "describe", 8)
        for width, height in ((37, 13), (110, 28), (46, 10)):
            term.resize(width, height)
            term.until_screen(_has_rule(width), f"{width}-cell composer rule after resize", timeout=8)
            visible = term.screen.visible()
            assert "[1]" in visible, f"image badge lost at {width}x{height}:\n{visible}"
            assert "image/png" in visible, f"image media type lost at {width}x{height}:\n{visible}"
            assert "describe" in visible, f"draft lost while an image was attached at {width}x{height}:\n{visible}"
            if width >= 80:
                assert "badge.png" in visible, f"image label lost at {width}x{height}:\n{visible}"
                assert "Alt+R remove last" in visible, f"image removal path lost at {width}x{height}:\n{visible}"
            _assert_cursor_on(term, "describe", 8)

        term.send(b"\x1br")  # Alt+R detaches without touching the draft or caret
        term.until("Removed badge.png", timeout=10)
        term.settle(0.2)
        assert "image/png" not in term.screen.visible(), term.screen.visible()
        _assert_cursor_on(term, "describe", 8)
        term.resize(60, 18)
        term.until_screen(_has_rule(60), "60-cell composer rule after detach resize", timeout=8)
        assert "image/png" not in term.screen.visible(), term.screen.visible()
        _assert_cursor_on(term, "describe", 8)

        term.send(b"\r")
        term.until("Error: no model configured or registered", timeout=15)
        term.settle(0.4)
        assert term.screen.document.count("describe") == 1, term.screen.document
    finally:
        term.close()


def test_resize_with_approval_overlay_keeps_gate_visible_and_decidable(tmp_path: Path) -> None:
    """A parked approval re-renders at every width and still resolves after shrinking."""
    call = {
        "id": "call-write",
        "type": "function",
        "function": {"name": "write", "arguments": json.dumps({"path": "approval-probe.txt", "content": "hello from approval\n"})},
    }
    replies = [
        openai_response(message={"role": "assistant", "content": None, "tool_calls": [call]}, finish="tool_calls"),
        openai_response(message={"role": "assistant", "content": "APPROVAL-DONE-MARKER", "tool_calls": []}, finish="stop"),
    ]
    with _loopback(replies) as server:
        workspace, state, session_id = _scene(tmp_path, server=server, approval_mode="ask", enabled=("files",), name="resize-approval")
        term = TuiPty(workspace, state, width=80, height=24, session_id=session_id)
        try:
            term.until("exact-mode")
            term.send(b"please write the approval probe\r")
            term.until("Tool approval required", timeout=30)
            assert "tool: write" in term.screen.visible()
            target = workspace / "approval-probe.txt"

            for width, height in ((36, 14), (110, 28), (48, 12)):
                term.resize(width, height)
                term.await_repaint()
                visible = term.screen.visible()
                lines = visible.splitlines()
                assert "Tool approval required" in visible, f"approval gate lost at {width}x{height}:\n{visible}"
                assert "tool: write" in visible, f"tool identity lost at {width}x{height}:\n{visible}"
                assert "allow once" in visible, f"decision hints lost at {width}x{height}:\n{visible}"
                gate = lines[next(index for index, line in enumerate(lines) if "Tool approval required" in line) :]
                overflow = [(len(line), line) for line in gate if len(line) > width]
                assert not overflow, f"gate rows exceed {width} cells: {overflow}"
                assert not target.exists(), f"approval decided without a decision at {width}x{height}"

            term.send(b"a")
            term.until_gone("Tool approval required", timeout=15)
            term.until("APPROVAL-DONE-MARKER", timeout=30)
            term.settle(0.5)
            assert target.read_text(encoding="utf-8") == "hello from approval\n"
            assert term.screen.document.count("APPROVAL-DONE-MARKER") == 1, term.screen.document

            # The composer is intact after the overlay and a further resize.
            term.resize(90, 20)
            term.await_repaint()
            term.send(b"post-approval draft")
            term.settle(0.3)
            assert "post-approval draft" in term.screen.visible(), term.screen.visible()
        finally:
            term.close()


def test_resize_keeps_long_scrollback_in_order_and_at_the_bottom(tmp_path: Path) -> None:
    """Forty committed rows survive four resizes exactly once, in order, below the live frame."""
    workspace, state, session_id = _scene(tmp_path, name="resize-scrollback")
    term = TuiPty(workspace, state, width=80, height=24, session_id=session_id)
    commands = [f"/bogus-{index:03d}" for index in range(40)]
    try:
        term.until("no model")
        for command in commands:
            term.send(command.encode() + b"\r")
            term.settle(0.06)
        term.until("Unknown command: /bogus-039", timeout=20)
        term.settle(0.3)

        def assert_history() -> None:
            document = term.screen.document
            counts = [(command, document.count(command)) for command in commands]
            assert all(count == 1 for _command, count in counts), [item for item in counts if item[1] != 1]
            positions = [document.index(command) for command in commands]
            assert positions == sorted(positions), "scrollback order changed across resize"

        assert_history()
        for width, height in ((50, 10), (100, 30), (30, 8), (90, 16)):
            term.resize(width, height)
            term.await_repaint()
            assert_history()
            visible = term.screen.visible()
            lines = visible.splitlines()
            assert lines[-1].endswith("no model"), f"footer not anchored at the bottom at {width}x{height}:\n{visible}"
            assert any(line == _RULE * (width - 1) for line in lines), f"composer frame missing at {width}x{height}:\n{visible}"
            assert "Unknown command: /bogus-039" in visible, f"newest committed row not visible at {width}x{height}:\n{visible}"
            assert "Unknown command: /bogus-000" not in visible, f"oldest row did not remain in scrollback at {width}x{height}:\n{visible}"

        term.send(b"draft-after-history")
        term.settle(0.3)
        assert "draft-after-history" in term.screen.visible(), term.screen.visible()
        assert_history()
    finally:
        term.close()
