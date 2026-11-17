"""Long-paste folding: badge display with exact true-text submit/copy/edit."""

from __future__ import annotations

from typing import Any

from voidcode.sdk.client import MAX_PROMPT_BYTES, MAX_PROMPT_CHARS, RunResult
from voidcode.tui.composer import Composer, ComposerAction
from voidcode.tui.keys import Key
from voidcode.tui.styles import Styles
from voidcode.tui.term import grapheme_spans
from voidcode.tui.terminal_app import CoreTuiApp


def _composer(width: int = 80, height: int = 5) -> Composer:
    return Composer(styles=Styles(None), width=width, max_height=height)


def _stack_trace(lines: int = 20) -> str:
    rows = ["Traceback (most recent call last):"]
    for i in range(lines - 1):
        rows.append(f'  File "app.py", line {i + 1}, in <module>\n    raise ValueError({i})')
    return "\n".join(rows)


def _cjk_text(lines: int = 20) -> str:
    return "\n".join(f"日本語テスト行{i} 中文测试 🎉 é_{i}" for i in range(lines))


def _render_text(composer: Composer) -> str:
    return "\n".join(composer.render())


def test_single_line_paste_unchanged() -> None:
    composer = _composer()
    composer.handle_key(Key("paste", "hello world"))
    assert composer.value == "hello world"
    assert composer.paste_blocks == ()
    assert composer.display_text == "hello world"
    assert composer.cursor == len("hello world")
    assert "[pasted" not in _render_text(composer)
    # Even a wrapping-tall single line never folds.
    wide = _composer(width=20, height=5)
    single = "x" * 200
    wide.handle_key(Key("paste", single))
    assert wide.value == single
    assert wide.paste_blocks == ()
    assert wide.display_text == single


def test_short_multiline_paste_stays_inline() -> None:
    composer = _composer(width=80, height=10)
    composer.handle_key(Key("paste", "a\nb"))
    assert composer.value == "a\nb"
    assert composer.paste_blocks == ()
    assert composer.display_text == "a\nb"
    assert "[pasted" not in _render_text(composer)


def test_long_paste_folds_by_viewport() -> None:
    trace = _stack_trace(20)
    small = _composer(width=80, height=5)
    small.handle_key(Key("paste", trace))
    assert small.value == trace
    assert len(small.paste_blocks) == 1
    start, end, collapsed = small.paste_blocks[0]
    assert (start, end, collapsed) == (0, len(trace), True)
    assert small.display_text != trace
    assert "[pasted 39 lines" in small.display_text
    rendered = _render_text(small)
    assert "[pasted" in rendered
    assert "Traceback" not in rendered
    assert len(small.render()) <= 9
    # Same fragment fits a tall viewport, so it stays inline.
    tall = _composer(width=80, height=60)
    tall.handle_key(Key("paste", trace))
    assert tall.paste_blocks == ()
    assert tall.display_text == trace


def test_width_drives_folding() -> None:
    fragment = "x" * 100 + "\n" + "y" * 100
    narrow = _composer(width=20, height=5)
    narrow.handle_key(Key("paste", fragment))
    assert len(narrow.paste_blocks) == 1
    wide = _composer(width=80, height=5)
    wide.handle_key(Key("paste", fragment))
    assert wide.paste_blocks == ()


def test_expand_collapse_toggle() -> None:
    composer = _composer()
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    assert composer.paste_blocks[0][2] is True
    composer.handle_key(Key("alt+e"))
    assert composer.paste_blocks[0][2] is False
    assert composer.display_text == trace
    assert "[pasted" not in _render_text(composer)
    composer.handle_key(Key("alt+e"))
    assert composer.paste_blocks[0][2] is True
    assert "[pasted" in composer.display_text


def test_edit_inside_expanded_edits_true_text() -> None:
    composer = _composer()
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    composer.handle_key(Key("alt+e"))
    assert composer.paste_blocks[0][2] is False
    for _ in range(3):
        composer.handle_key(Key("left"))
    before = composer.value
    cursor = composer.cursor
    composer.handle_key(Key("E", text="E"))
    assert composer.value == before[:cursor] + "E" + before[cursor:]
    assert len(composer.paste_blocks) == 1
    assert composer.paste_blocks[0][2] is False
    # Collapse again: badge byte count reflects the edit.
    composer.handle_key(Key("alt+e"))
    assert composer.paste_blocks[0][2] is True
    nbytes = len(composer.value.encode("utf-8"))
    assert f"{nbytes} bytes" in composer.display_text


def test_backspace_at_badge_deletes_true_span() -> None:
    composer = _composer()
    composer.handle_key(Key("a", text="a"))
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    assert composer.value == "a" + trace
    composer.handle_key(Key("backspace"))
    assert composer.value == "a"
    assert composer.paste_blocks == ()
    assert composer.display_text == "a"


def test_delete_at_badge_start_deletes_true_span() -> None:
    composer = _composer()
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    composer.handle_key(Key("Z", text="Z"))
    assert composer.value == trace + "Z"
    # Cursor after Z; move left over Z then jump over the badge to its start.
    composer.handle_key(Key("left"))
    assert composer.cursor == len(trace)
    composer.handle_key(Key("left"))
    assert composer.cursor == 0
    composer.handle_key(Key("delete"))
    assert composer.value == "Z"
    assert composer.paste_blocks == ()


def test_alt_x_deletes_block() -> None:
    composer = _composer()
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    composer.handle_key(Key("alt+x"))
    assert composer.value == ""
    assert composer.paste_blocks == ()


def test_copy_returns_exact_bytes() -> None:
    composer = _composer()
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    outcome = composer.handle_key(Key("alt+c"))
    assert outcome.action == ComposerAction.COPY
    assert outcome.text == trace
    assert outcome.text.encode("utf-8") == trace.encode("utf-8")
    # No block under the caret: no copy, draft untouched.
    plain = _composer()
    plain.handle_key(Key("a", text="a"))
    outcome = plain.handle_key(Key("alt+c"))
    assert outcome.action == ComposerAction.NONE
    assert plain.value == "a"


def test_submit_restores_exact_original_never_badge() -> None:
    composer = _composer()
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    composer.handle_key(Key("!", text="!"))
    expected = trace + "!"
    outcome = composer.handle_key(Key("enter"))
    assert outcome.action == ComposerAction.SUBMIT
    assert outcome.text == expected
    assert "[pasted" not in outcome.text
    assert composer.value == ""
    assert composer.paste_blocks == ()
    assert composer.history == (expected,)


def test_cjk_long_paste_folds_and_preserves_bytes() -> None:
    text = _cjk_text(20)
    composer = _composer(width=40, height=5)
    composer.handle_key(Key("paste", text))
    assert composer.value == text
    assert len(composer.paste_blocks) == 1
    nbytes = len(text.encode("utf-8"))
    assert nbytes > len(text)
    assert f"{nbytes} bytes" in composer.display_text
    outcome = composer.handle_key(Key("alt+c"))
    assert outcome.action == ComposerAction.COPY
    assert outcome.text == text
    # Atomic badge jump never lands mid-grapheme.
    assert composer.cursor == len(text)
    composer.handle_key(Key("left"))
    assert composer.cursor == 0
    composer.handle_key(Key("right"))
    assert composer.cursor == len(text)
    for start, end in grapheme_spans(composer.value):
        assert 0 <= start < end <= len(text)
    # Expanded CJK editing stays grapheme-snapped.
    composer.handle_key(Key("alt+e"))
    composer.handle_key(Key("left"))
    ends = {end for _, end in grapheme_spans(composer.value)}
    assert composer.cursor in ends or composer.cursor == 0
    outcome = composer.handle_key(Key("enter"))
    assert outcome.action == ComposerAction.SUBMIT
    assert outcome.text.encode("utf-8") == text.encode("utf-8")


def test_multi_segment_pastes_fold_independently() -> None:
    composer = _composer(width=80, height=6)
    first = _stack_trace(12)
    second = _cjk_text(12)
    composer.handle_key(Key("paste", first))
    assert len(composer.paste_blocks) == 1
    for char in "\nMID\n":
        composer.handle_key(Key(char, text=char))
    composer.handle_key(Key("paste", second))
    assert len(composer.paste_blocks) == 2
    assert composer.value == first + "\nMID\n" + second
    assert composer.display_text.count("[pasted") == 2
    assert len(composer.render()) <= 11
    # Move to the first badge and expand only it.
    while composer.cursor > len(first):
        composer.handle_key(Key("left"))
    assert composer.cursor == len(first)
    composer.handle_key(Key("alt+e"))
    blocks = composer.paste_blocks
    assert blocks[0][2] is False
    assert blocks[1][2] is True
    outcome = composer.handle_key(Key("enter"))
    assert outcome.action == ComposerAction.SUBMIT
    assert outcome.text == first + "\nMID\n" + second


def test_history_browse_preserves_draft_blocks() -> None:
    composer = _composer()
    composer.handle_key(Key("a", text="a"))
    outcome = composer.handle_key(Key("enter"))
    assert outcome.action == ComposerAction.SUBMIT
    trace = _stack_trace(20)
    composer.handle_key(Key("paste", trace))
    assert len(composer.paste_blocks) == 1
    composer.handle_key(Key("up"))
    assert composer.value == "a"
    assert composer.paste_blocks == ()
    composer.handle_key(Key("down"))
    assert composer.value == trace
    assert len(composer.paste_blocks) == 1
    assert "[pasted" in composer.display_text


class _StubTerminal:
    width = 100
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.frames: list[list[str]] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.rows))


class _StubClient:
    def __init__(self) -> None:
        self.runs: list[str] = []

    def command_catalog(self) -> Any:
        from voidcode.sdk.client import CommandCatalog

        return CommandCatalog(revision_id="rev-1", commands=())

    def run(self, session_id: str, spec: Any) -> RunResult:
        self.runs.append(spec.prompt)
        return RunResult(session_id=session_id, run_id="r1", revision_id="rev-1", status="completed")


def _app() -> tuple[CoreTuiApp, _StubTerminal, _StubClient]:
    terminal = _StubTerminal()
    client = _StubClient()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.catalog_cache = ()
    app.status_line = ""
    return app, terminal, client


def test_oversize_prompt_refused_explicitly_without_send() -> None:
    app, _, client = _app()
    assert MAX_PROMPT_CHARS == 65536
    assert MAX_PROMPT_BYTES == 64 * 1024
    big = "x" * (MAX_PROMPT_CHARS + 10)
    for chunk_start in range(0, len(big), 5000):
        app._handle_composer_key(Key("paste", text=big[chunk_start : chunk_start + 5000]))
    assert app.composer.value == big
    app._handle_composer_key(Key("enter"))
    assert client.runs == []
    assert app.composer.value == big
    assert "too large" in app.status_line
    assert "draft kept" in app.status_line
    assert "nothing sent" in app.status_line
    # No double-send on retry; shortening then allows exactly one send.
    app._handle_composer_key(Key("enter"))
    assert client.runs == []
    assert app.composer.value == big
    app.composer.set_text("hi")
    app.status_line = ""
    app._handle_composer_key(Key("enter"))
    assert client.runs == ["hi"]


def test_oversize_keeps_folded_blocks() -> None:
    app, _, client = _app()
    trace = _stack_trace(20)
    app._handle_composer_key(Key("paste", text=trace))
    assert len(app.composer.paste_blocks) == 1
    big_single = "y" * (MAX_PROMPT_BYTES + 10)
    app._handle_composer_key(Key("paste", text=big_single))
    assert len(app.composer.paste_blocks) == 1
    app._handle_composer_key(Key("enter"))
    assert client.runs == []
    assert len(app.composer.paste_blocks) == 1
    assert app.composer.value == trace + big_single
    assert "too large" in app.status_line


def test_copy_oversize_clipboard_explicit() -> None:
    app, _, _ = _app()
    huge = "\n".join(f"line-{i}-" + "z" * 80 for i in range(2000))
    assert len(huge.encode("utf-8")) > 100_000
    app._handle_composer_key(Key("paste", text=huge))
    assert len(app.composer.paste_blocks) == 1
    app._handle_composer_key(Key("alt+c"))
    assert "Copy failed" in app.status_line
    assert "exceeds" in app.status_line
    assert app.composer.value == huge
    assert len(app.composer.paste_blocks) == 1


def test_copy_success_reports_bytes(monkeypatch: Any) -> None:
    import voidcode.tui.terminal_app as terminal_app_module

    app, _, _ = _app()
    trace = _stack_trace(20)
    app._handle_composer_key(Key("paste", text=trace))
    seen: list[str] = []
    monkeypatch.setattr(terminal_app_module, "copy_to_clipboard", lambda text: seen.append(text))
    app._handle_composer_key(Key("alt+c"))
    assert seen == [trace]
    assert "Copied" in app.status_line
    assert str(len(trace.encode("utf-8"))) in app.status_line
    assert app.composer.value == trace
