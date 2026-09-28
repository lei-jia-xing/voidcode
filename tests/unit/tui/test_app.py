"""``TuiApp``'s queue-drain rules: the poll replay and the failed-answer re-open.

These are the two app-loop behaviours the pure ``SessionView`` cannot express --
they are decided where the queue is drained. Everything here is headless: no
terminal, no runtime, no thread. The app is built with ``__new__`` and only the
attributes the exercised path touches are set, so the assertions stay on the
observable view (assistant blocks, transcript rows, pending overlay identity).
"""

from __future__ import annotations

from types import SimpleNamespace

from voidcode.runtime.contracts import RuntimeStreamChunk
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.session import SessionState
from voidcode.tui.app import KeyBindingError, TuiApp, _PolledChunks, parse_keymap
from voidcode.tui.events import QuestionRequest, SessionView
from voidcode.tui.transcript import AssistantBlock

from .conftest import theme as resolve_test_theme

ANSWER = "The answer is 42."
SESSION = "session-4f2a1b2c9d"


def _session_state() -> SessionState:
    return SessionState(session=SimpleNamespace(id=SESSION), status="completed", turn=1, metadata={})  # type: ignore[arg-type]


def _chunk(kind: str, **kwargs: object) -> RuntimeStreamChunk:
    return RuntimeStreamChunk(kind=kind, session=_session_state(), **kwargs)  # type: ignore[arg-type]


def _output_chunk(output: str) -> RuntimeStreamChunk:
    return _chunk("output", output=output)


def _event_chunk(event_type: str, sequence: int) -> RuntimeStreamChunk:
    event = EventEnvelope(session_id=SESSION, sequence=sequence, event_type=event_type, source="runtime", payload={})
    return _chunk("event", event=event)


def _app() -> TuiApp:
    app = TuiApp.__new__(TuiApp)
    view = SessionView(theme=resolve_test_theme(), width=80)
    app._view = view
    app._session_id = SESSION
    app._cancel_requested = False
    app._overlay = None
    app._dirty = False
    app._tracked_tasks = set()
    return app


def _assistant_blocks(app: TuiApp) -> list[AssistantBlock]:
    return [block for block in app._view.transcript().blocks if isinstance(block, AssistantBlock)]  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# F1 -- a polled replay must not reprint the answer
# ---------------------------------------------------------------------------


def test_poll_replay_does_not_reprint_the_answer() -> None:
    """The 1 s poll replays the stored answer; the tape must keep one copy."""
    app = _app()
    view = app._view
    assert view is not None
    view.transcript().add(AssistantBlock(text=ANSWER, settled=True))
    view.finish_stream("completed")  # the turn settled; the poll's output is history

    replay = _PolledChunks(SESSION, (_event_chunk("runtime.request_received", 1), _output_chunk(ANSWER)))
    for _ in range(3):
        app._consume_polled(replay)

    blocks = _assistant_blocks(app)
    assert [block.text for block in blocks] == [ANSWER]
    # The crucial half: no permanently-unsettled duplicate, so the live tail
    # cannot grow without bound (`Transcript.live_rows` returns every unsettled block).
    assert view.transcript().live_rows() == []


def test_poll_replay_settles_a_genuinely_new_answer() -> None:
    """A replay may be the first place the tape sees an answer; it lands settled."""
    app = _app()
    replay = _PolledChunks(SESSION, (_output_chunk(ANSWER),))

    app._consume_polled(replay)

    blocks = _assistant_blocks(app)
    assert [block.text for block in blocks] == [ANSWER]
    assert blocks[0].settled is True
    assert app._view.transcript().live_rows() == []  # type: ignore[union-attr]


def test_poll_replay_for_another_session_is_ignored() -> None:
    app = _app()
    app._consume_polled(_PolledChunks("session-other", (_output_chunk(ANSWER),)))
    assert _assistant_blocks(app) == []


def test_live_output_chunk_still_lands_unsettled() -> None:
    """The non-replay path is unchanged: the turn's own answer stays live."""
    app = _app()
    app._consume_output(ANSWER)
    blocks = _assistant_blocks(app)
    assert [block.text for block in blocks] == [ANSWER]
    assert blocks[0].settled is False


def _render_app(*, height: int = 8, width: int = 60, committed: list[str] | None = None):
    """A headless ``TuiApp`` wired for ``_render``, with a recording terminal."""
    from pathlib import Path

    from voidcode.tui.composer import Composer
    from voidcode.tui.region import LiveRegion
    from voidcode.tui.statusline import StatusLine

    rows_out = committed if committed is not None else []
    theme = resolve_test_theme()
    app = _app()
    app._theme = theme
    app._view = SessionView(theme=theme, width=width)
    app._status = StatusLine(theme, width)
    app._composer = Composer(theme=theme, width=width)
    app._expand_notice = ""
    app._config = SimpleNamespace(reasoning_effort="off")
    app._approval_mode = "ask"
    app._model = "test-model"
    app._lsp_label = ""
    app._context_window = 0
    app._context_tokens = 0
    app._cost_usd = 0.0
    app._session_effort = ""
    app._workspace = Path(".")

    class FakeTerminal:
        def __init__(self) -> None:
            self.height = height
            self.width = width
            self.is_tty = True
            self.alt_screen = False

        def commit_rows(self, rows: object) -> None:
            rows_out.extend(rows)  # type: ignore[arg-type]

        def paint_frame(self, rows: object) -> None:
            pass

    app._term = FakeTerminal()  # type: ignore[assignment]
    app._region = LiveRegion(FakeTerminal.commit_rows.__get__(app._term), max_rows=height, paint=lambda rows: None)
    return app


def test_an_overflowing_stream_commits_no_row_twice() -> None:
    """``TuiApp._render``'s sequence, on an answer taller than the terminal.

    The live tail overflows the region mid-stream (so rows are written), and the
    same block then settles and is handed to ``commit`` whole. Each rendered row
    must reach scrollback once.
    """
    committed: list[str] = []
    app = _render_app(committed=committed)

    assert app._view is not None
    transcript = app._view.transcript()
    block = AssistantBlock(text="", settled=False)
    transcript.add(block)

    # A prior settled block, so the settle batch carries take_settled's blank separator.
    transcript.blocks.insert(0, AssistantBlock(text="earlier", settled=True))
    app._render()

    lines = [f"answer line {index:03d}" for index in range(40)]
    for count in range(1, len(lines) + 1):
        block.text = "\n\n".join(lines[:count])
        app._render()
    block.text = "\n\n".join(lines)
    transcript.mark_settled()
    app._render()
    app._render()

    duplicates = {row for row in committed if committed.count(row) > 1 and row.strip()}
    answer_duplicates = {row for row in duplicates if "answer line" in row}
    assert answer_duplicates == set(), sorted(answer_duplicates)


def test_an_overflowing_stream_loses_no_row_when_the_turn_fails() -> None:
    """A mid-turn failure settles the tape; no streamed row may be dropped.

    The verifier's loss case: prose overflows the terminal while a tool card
    holds the head, the transport fails, and the settle batch re-lays the rows
    out under it. Every content row on the final tape must be delivered.
    """
    committed: list[str] = []
    app = _render_app(height=24, width=100, committed=committed)
    view = app._view
    assert view is not None
    transcript = view.transcript()

    transcript.blocks.insert(0, AssistantBlock(text="📄 read", settled=True))
    block = transcript.add(AssistantBlock(text="", settled=False))
    app._render()
    for index in range(20):
        block.text = "\n\n".join(f"MESSAGE-{n}" for n in range(index + 1))
        app._render()
    view.finish_stream("failed", error=RuntimeError("transport down"))
    app._render()
    app._render()

    delivered = {row.strip() for row in committed} | {row.strip() for row in (app._region.pending_rows() if app._region else ())}
    for index in range(20):
        assert f"MESSAGE-{index}" in delivered, (index, sorted(delivered))


def test_a_tool_card_settling_mid_answer_keeps_every_row() -> None:
    """A tool card settling above live prose re-lays out the written prefix.

    The card's rows move when it completes, so the rows already written diverge;
    the frozen-presentation consequence is a duplicate, but no row may be lost.
    This is the shape the round-2 report reproduced at a normal terminal.
    """
    from voidcode.runtime.events import EventEnvelope

    def envelope(event_type: str, payload: dict, sequence: int, source: str = "runtime") -> EventEnvelope:
        return EventEnvelope(session_id="s-1", sequence=sequence, event_type=event_type, source=source, payload=payload)

    committed: list[str] = []
    app = _render_app(height=8, width=60, committed=committed)
    view = app._view
    assert view is not None
    view.apply_event(envelope("runtime.request_received", {"prompt": "go"}, 1))
    app._render()
    view.apply_event(
        envelope(
            "runtime.tool_started",
            {"tool": "read", "tool_call_id": "c1", "display": {"kind": "read", "path": "f.py"}, "status": "running"},
            2,
        )
    )
    app._render()
    for index in range(20):
        view.apply_event(envelope("graph.provider_stream", {"channel": "text", "kind": "delta", "text": f"MESSAGE-{index}\n\n"}, index + 3, "graph"))
        app._render()
    view.apply_event(
        envelope(
            "runtime.tool_completed",
            {"tool": "read", "tool_call_id": "c1", "status": "ok", "content": "TOOLOUT-1", "display": {"kind": "read", "path": "f.py"}},
            99,
        )
    )
    view.finish_stream("completed")
    app._render()
    app._render()

    delivered = {row.strip() for row in committed} | {row.strip() for row in (app._region.pending_rows() if app._region else ())}
    for index in range(20):
        assert f"MESSAGE-{index}" in delivered, (index, sorted(delivered))


def test_a_resize_re_commits_no_scrollback_row() -> None:
    """A width change re-lays the tape out; scrollback is append-only, so the
    rows already written must not be handed over again.
    """
    committed: list[str] = []
    app = _render_app(height=10, width=60, committed=committed)
    assert app._view is not None and app._term is not None
    block = app._view.transcript().add(AssistantBlock(text="", settled=False))
    lines = [f"resize line {index:03d}" for index in range(20)]

    for width in (60, 30, 60, 100):
        app._term.width = width
        app._apply_resize()
        block.text = "\n\n".join(lines)
        app._render()
    app._view.transcript().mark_settled()
    app._render()

    duplicates = {row for row in committed if committed.count(row) > 1 and row.strip()}
    assert duplicates == set(), sorted(duplicates)


def test_a_thinking_block_collapsing_keeps_every_row() -> None:
    """A thinking block collapsing on settle re-lays out the rows above it.

    The collapse drops rows the ledger may already have written; scrollback is
    append-only, so the honest outcome is the divergent-append, but the answer
    rows that follow must still be delivered exactly once.
    """
    committed: list[str] = []
    app = _render_app(height=8, width=60, committed=committed)
    view = app._view
    assert view is not None
    transcript = view.transcript()

    from voidcode.tui.transcript import ThinkingBlock

    thinking = transcript.add(ThinkingBlock(text="\n\n".join(f"REASON-{n}" for n in range(30)), settled=False))
    answer = transcript.add(AssistantBlock(text="", settled=False))
    app._render()
    for index in range(10):
        answer.text = "\n\n".join(f"MESSAGE-{n}" for n in range(index + 1))
        app._render()
    assert isinstance(thinking, ThinkingBlock)
    thinking.settled = True  # the collapse re-lays out the tape
    transcript.mark_settled()
    app._render()
    app._render()

    committed_plain = {row.strip() for row in committed}
    live_plain = {row.strip() for row in (app._region.pending_rows() if app._region else ())}
    delivered = committed_plain | live_plain
    for index in range(10):
        assert f"MESSAGE-{index}" in delivered, (index, sorted(delivered))


# ---------------------------------------------------------------------------
# Stuck-session re-open -- a failed answer round trip
# ---------------------------------------------------------------------------


def test_failed_answer_reopens_the_still_pending_overlay() -> None:
    """A failed answer leaves the request pending; the app must re-open it."""
    app = _app()
    view = app._view
    assert view is not None
    request = QuestionRequest(request_id="req-1", questions=())
    view._pending = request
    assert view.take_pending_overlay() is request  # the wizard opened it
    view.resolve_overlay("req-1")  # the user answered; the request is still live
    assert view.take_pending_overlay() is None  # ...but not handed out twice

    opened: list[QuestionRequest] = []
    app._take_pending_overlay = lambda: opened.append(view.take_pending_overlay())  # type: ignore[method-assign]

    app._fail_stream(RuntimeError("transport down"))

    assert opened == [request]


def test_failed_turn_without_a_pending_overlay_is_a_no_op() -> None:
    app = _app()
    view = app._view
    assert view is not None
    view.transcript().add(AssistantBlock(text="partial", settled=False))

    app._fail_stream(RuntimeError("transport down"))

    assert app._overlay is None
    assert view.take_pending_overlay() is None
    assert view.view_state().state == "Failed"


def test_failed_turn_keeps_the_error_row_and_settles_the_tape() -> None:
    app = _app()
    view = app._view
    assert view is not None
    block = view.transcript().add(AssistantBlock(text="partial", settled=False))

    app._fail_stream(RuntimeError("transport down"))

    assert block.settled is True
    from voidcode.tui.transcript import ErrorBlock

    assert any(isinstance(item, ErrorBlock) for item in view.transcript().blocks)


# ---------------------------------------------------------------------------
# Keymap -- impossible bindings fail loudly
# ---------------------------------------------------------------------------


def test_a_binding_to_a_key_the_decoder_cannot_emit_fails_loudly() -> None:
    for spec in ("f13", "clear_all", "unknown"):
        try:
            parse_keymap({spec: "tools_expand"})
        except KeyBindingError:
            continue
        raise AssertionError(f"{spec!r} should not be bindable")
