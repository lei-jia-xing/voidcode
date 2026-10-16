from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voidcode.command.native import (
    INIT_PROMPT,
    NATIVE_COMMANDS,
    NATIVE_COMMANDS_BY_NAME,
    is_native_command,
)
from voidcode.core.turns import FinalTurn, ModelTurnFact, TurnRequest
from voidcode.runtime.contracts import ReviewFileDiff
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.term import Terminal
from voidcode.tui.transcript import NoticeBlock


class _DummyProducer:
    def __init__(self, answer: str = "ok") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def produce(self, request: TurnRequest, tool_results: tuple[object, ...], *, session: object):  # type: ignore[no-untyped-def]
        _ = tool_results, session
        self.prompts.append(request.prompt)
        return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output=self.answer)


class _MockTuiRuntime:
    def __init__(self) -> None:
        self.compact_calls: list[str] = []
        self.diff_calls = 0
        self.btw_calls: list[tuple[str, str]] = []
        self.shell_calls: list[tuple[str | None, str]] = []
        self.sessions_created: list[dict[str, object]] = []

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(context_window=None)

    def review_diff_all(self) -> ReviewFileDiff:
        self.diff_calls += 1
        return ReviewFileDiff(root="/workspace", path=".", state="clean", diff="")

    def compact_session(self, *, session_id: str) -> dict[str, object]:
        self.compact_calls.append(session_id)
        return {"session_id": session_id, "compacted": True}

    def ask_side_question(self, *, session_id: str, question: str) -> dict[str, object]:
        self.btw_calls.append((session_id, question))
        return {"session_id": session_id, "question": question, "answer": "side answer"}

    def run_user_shell(self, *, session_id: str | None, command: str) -> dict[str, object]:
        self.shell_calls.append((session_id, command))
        return {"session_id": session_id or "shell-1", "command": command, "exit_code": 0, "status": "completed"}

    def list_sessions(self) -> list[SimpleNamespace]:
        return []

    def session_status_panel(self, *, session_id: str) -> None:
        return None

    def copy_targets(self, *, session_id: str) -> tuple[object, ...]:
        return ()

    def session_context_usage(self, *, session_id: str, expand_items: bool = False) -> SimpleNamespace:
        return SimpleNamespace(session_id=session_id, used_tokens=10, segments=[])

    def get_goal(self, *, session_id: str) -> None:
        return None

    def cancel_session(self, *, session_id: str) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


def _last_notice(app: TuiApp) -> str:
    assert app._view is not None
    notices = [b.text for b in app._view.transcript().blocks if isinstance(b, NoticeBlock)]
    return notices[-1] if notices else ""


def _make_tui_app(tmp_path: Path, runtime: _MockTuiRuntime | None = None) -> tuple[TuiApp, Terminal, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    app = TuiApp(workspace, runtime=runtime or _MockTuiRuntime())
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    return app, terminal, output


def test_table_to_catalog_parity(tmp_path: Path) -> None:
    """Every NATIVE_COMMANDS entry appears in list_command_summaries with source=native; no extras."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        summaries = runtime.list_command_summaries()

    native_summaries = [s for s in summaries if s.source == "native"]
    assert len(native_summaries) == len(NATIVE_COMMANDS)

    # Names match 1:1 and ordering is strictly preserved
    table_names = [cmd.name for cmd in NATIVE_COMMANDS]
    catalog_names = [s.name for s in native_summaries]
    assert catalog_names == table_names

    # Descriptions match verbatim
    table_descriptions = {cmd.name: cmd.description for cmd in NATIVE_COMMANDS}
    for summary in native_summaries:
        assert summary.description == table_descriptions[summary.name]


def test_table_to_tui_dispatch_parity(tmp_path: Path) -> None:
    """Every entry dispatches via NATIVE_COMMANDS; unknown commands are intercepted (Codex parity)."""
    runtime = _MockTuiRuntime()
    app, terminal, output = _make_tui_app(tmp_path, runtime)
    submitted_prompts: list[str] = []
    app._start_prompt = submitted_prompts.append
    app._session_id = "session-test"

    try:
        # Unknown command is intercepted: notice, draft restored, nothing submitted
        app._submit("/unknown_custom_macro with args")
        assert submitted_prompts == []
        assert _last_notice(app) == "Unrecognized command '/unknown_custom_macro'. Type \"/\" for a list of supported commands."
        assert app._composer is not None
        assert app._composer.value == "/unknown_custom_macro with args"

        # Name containing `/` is literal text (upstream exception) — submitted
        app._submit("/foo/bar")
        assert submitted_prompts == ["/foo/bar"]
        submitted_prompts.clear()

        # Bare `/` (empty name) is literal text — submitted
        app._submit("/")
        assert submitted_prompts == ["/"]
        submitted_prompts.clear()

        # Each command in NATIVE_COMMANDS is recognized
        for cmd in NATIVE_COMMANDS:
            if cmd.name == "!":
                continue
            assert is_native_command(cmd.name)

        # Arg rule validation checks
        # Rule "none": rejects non-empty args
        app._submit("/compact unexpected")
        assert _last_notice(app) == "/compact does not accept arguments"

        app._submit("/diff unexpected")
        assert _last_notice(app) == "/diff does not accept arguments"

        app._submit("/status unexpected")
        assert _last_notice(app) == "/status does not accept arguments"

        app._submit("/copy unexpected")
        assert _last_notice(app) == "/copy does not accept arguments"

        app._submit("/init unexpected")
        assert _last_notice(app) == "/init does not accept arguments"

        # Rule "enum": rejects unallowed enum values
        app._submit("/plan bad_arg")
        assert _last_notice(app) == "/plan accepts only 'off'"

        app._submit("/context bad_arg")
        assert _last_notice(app) == "/context accepts only 'all'"
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_native_init_submits_verbatim_prompt_and_busy_gates(tmp_path: Path) -> None:
    """Native /init submits the fixed AGENTS.md generation prompt as a normal turn and is busy-gated."""
    runtime = _MockTuiRuntime()
    app, terminal, output = _make_tui_app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    app._session_id = "session-1"

    try:
        # Idle submission dispatches the exact prompt
        app._submit("/init")
        assert len(submitted) == 1
        assert submitted[0] == INIT_PROMPT
        assert "Generate a file named AGENTS.md" in submitted[0]
        submitted.clear()

        # Busy gated: blocked while streaming
        app._streaming = True
        app._submit("/init")
        assert submitted == []
        assert _last_notice(app) == "'/init' is disabled while a task is in progress."
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_native_dispatch_and_catalog_parity(tmp_path: Path) -> None:
    """Native commands route to native handlers and appear in catalog as native."""
    runtime = _MockTuiRuntime()
    app, terminal, output = _make_tui_app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    app._session_id = "session-test"

    try:
        # /compact is routed to native compact (and fails with arg notice when args passed,
        # instead of expanding the custom compact.md template).
        app._submit("/compact some argument")
        assert _last_notice(app) == "/compact does not accept arguments"
        assert submitted == []

        # /init is routed to native init (submits INIT_PROMPT, NOT custom init.md)
        app._submit("/init")
        assert submitted == [INIT_PROMPT]
        assert "Custom init" not in submitted[0]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()

    # Catalog inspection: native entry wins over colliding markdown command
    producer = _DummyProducer()
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=producer) as vc_runtime:
        summaries = vc_runtime.list_command_summaries()
        summary_map = {s.name: s for s in summaries}
        assert summary_map["compact"].source == "native"
        assert summary_map["compact"].description == NATIVE_COMMANDS_BY_NAME["compact"].description
        assert summary_map["init"].source == "native"
        assert summary_map["init"].description == NATIVE_COMMANDS_BY_NAME["init"].description


def test_unknown_slash_command_preserves_draft_and_blocks_submit(tmp_path: Path) -> None:
    """Unknown `/foo` (Codex chat_composer.rs:3072-3094): notice + draft kept + no submit; natives/`!` unaffected."""
    runtime = _MockTuiRuntime()
    app, terminal, output = _make_tui_app(tmp_path, runtime)
    submitted: list[str] = []
    app._start_prompt = submitted.append
    app._session_id = "session-test"
    assert app._composer is not None

    try:
        # End-to-end through the key handler: composer clears on Enter, _submit restores the draft.
        app._composer.set_text("/foo some args")
        app._handle_key(Key("enter"))
        assert submitted == []
        assert _last_notice(app) == "Unrecognized command '/foo'. Type \"/\" for a list of supported commands."
        assert app._composer.value == "/foo some args"

        # Native /diff still dispatches (state=clean mock -> notice, no submit).
        app._submit("/diff")
        assert submitted == []
        assert runtime.diff_calls == 1
        assert _last_notice(app) == "/diff: no changes"

        # `!` escape still runs before slash routing.
        app._submit("!echo hi")
        assert submitted == []
        assert runtime.shell_calls == [("session-test", "echo hi")]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()
