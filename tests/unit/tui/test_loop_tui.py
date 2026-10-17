from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from voidcode.command.loop import USAGE_MESSAGE
from voidcode.runtime.background.models import (
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskState,
    ScheduleSummary,
)
from voidcode.tui.app import TuiApp
from voidcode.tui.term import Terminal
from voidcode.tui.transcript import NoticeBlock


class _TuiLoopMockRuntime:
    def __init__(self) -> None:
        self.schedules: dict[str, ScheduleSummary] = {}
        self.fired: list[str] = []
        self._next_id = 1

    def create_schedule(
        self,
        *,
        schedule: str,
        prompt: str,
        parent_session_id: str | None = None,
        enabled: bool = True,
        expires_at: int | None = None,
        schedule_id: str | None = None,
    ) -> ScheduleSummary:
        from voidcode.runtime.background.schedule_expr import human_schedule, parse_schedule

        expr = parse_schedule(schedule)
        sched_id = schedule_id or f"sched-{self._next_id}"
        self._next_id += 1
        summary = ScheduleSummary(
            id=sched_id,
            workspace=".",
            schedule_text=schedule,
            kind=expr.kind,
            prompt=prompt,
            parent_session_id=parent_session_id,
            enabled=enabled,
            next_fire_at=1000,
            running_task_id=None,
            created_at=500,
            expires_at=expires_at,
            human_schedule=human_schedule(expr),
        )
        self.schedules[sched_id] = summary
        return summary

    def fire_schedule(self, *, schedule_id: str) -> BackgroundTaskState | None:
        self.fired.append(schedule_id)
        return BackgroundTaskState(
            task=BackgroundTaskRef(id=f"task-fire-{schedule_id}"),
            status="queued",
            request=BackgroundTaskRequestSnapshot(prompt="test", session_id=None, parent_session_id=None),
        )

    def provider_readiness(self, *, session_id: str | None) -> SimpleNamespace:
        return SimpleNamespace(context_window=None)

    def current_lsp_state(self) -> SimpleNamespace:
        return SimpleNamespace(mode="disabled", servers={})

    def run_config(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(approval_mode="ask")

    def __exit__(self, *_args: object) -> None:
        return None


def _last_notice(app: TuiApp) -> str:
    notices = [b.text for b in app._view.transcript().blocks if isinstance(b, NoticeBlock)]
    return notices[-1] if notices else ""


def _make_app(tmp_path: Path, runtime: _TuiLoopMockRuntime) -> tuple[TuiApp, Terminal, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TuiApp(workspace, runtime=runtime)
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    return app, terminal, output


def test_tui_submit_loop_usage(tmp_path: Path) -> None:
    runtime = _TuiLoopMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._submit("/loop")
        notice = _last_notice(app)
        assert notice == USAGE_MESSAGE
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()


def test_tui_submit_loop_success(tmp_path: Path) -> None:
    runtime = _TuiLoopMockRuntime()
    app, terminal, output = _make_app(tmp_path, runtime)
    try:
        app._session_id = "test-session"
        app._submit("/loop 5m check the deploy")
        notice = _last_notice(app)
        assert "check the deploy" in notice
        assert "every 5m" in notice
        assert "job ID:" in notice
        assert len(runtime.schedules) == 1
        sched_id = next(iter(runtime.schedules.keys()))
        assert runtime.fired == [sched_id]
    finally:
        app._shutdown(terminal)
        terminal.close()
        output.close()
