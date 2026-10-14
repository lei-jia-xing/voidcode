import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import get_args

import pytest

from tests.provider_loopback import (
    OpenAILoopbackServer,
    configure_openai_provider,
    openai_response,
)
from voidcode.runtime.config.models import (
    VALID_TUI_COMPOSER_CURSOR_SHAPES,
    VALID_TUI_COMPOSER_STYLES,
    VALID_TUI_THEME_MODES,
    TuiSymbolsPreset,
)
from voidcode.tui.statusline import statusline_profiles
from voidcode.tui.term import visible_width
from voidcode.tui.theme import resolve_theme

pytestmark = pytest.mark.skipif(os.name != "posix" or shutil.which("tmux") is None, reason="requires a POSIX tmux terminal")


def _tmux(socket: Path, *args: str, env: dict[str, str], timeout: float = 5) -> str:
    result = subprocess.run(
        ["tmux", "-S", str(socket), "-f", "/dev/null", *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    if result.returncode:
        pytest.fail(f"tmux {args!r} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


_QUESTION_CHILD = r"""
import os
from pathlib import Path

from voidcode.core.turns import FinalTurn, ModelTurnFact, ToolTurn
from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tools.contracts import ToolCall
from voidcode.tui.app import TuiApp


class QuestionProducer:
    def produce(self, request, tool_results, *, session):
        fact = ModelTurnFact(request.run_step, "deterministic", request.prompt)
        _ = session
        questions = (
            {
                "question": "Choose a path",
                "header": "Path",
                "options": [{"label": "e\u0301"}, {"label": "Other"}],
                "multiple": False,
            },
            {
                "question": "Cancel at 20x6",
                "header": "At minimum",
                "options": [{"label": "Yes"}],
                "multiple": False,
            },
            {
                "question": "Cancel below min",
                "header": "Below minimum",
                "options": [{"label": "Yes"}],
                "multiple": False,
            },
        )
        if len(tool_results) >= len(questions):
            return FinalTurn(output="question answers and cancellations recorded", facts=(fact,))
        return ToolTurn(
            calls=(
                ToolCall(
                    tool_name="question",
                    arguments={"questions": [questions[len(tool_results)]]},
                ),
            ),
            facts=(fact,),
        )

workspace = Path(os.environ["NATIVE_WORKSPACE"])
runtime = VoidCodeRuntime(
    workspace=workspace,
    turn_producer=QuestionProducer(),
    config=RuntimeConfig(approval_mode="yolo"),
)
raise SystemExit(TuiApp(workspace, runtime=runtime).run())
"""


def test_native_tui_survives_small_terminal_sizes_and_restores_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    workspace = root / "workspace"
    home = root / "home"
    config_home = root / "config"
    local_app_data = root / "localappdata"
    tmux_tmpdir = root / "tmux"
    for directory in (workspace, home, config_home, local_app_data, tmux_tmpdir):
        directory.mkdir()
    earlier_content = "".join(f"EARLIER_ROW_{index:04d}\n" for index in range(1200)) + "NATIVE_PTY_READY\n"
    (workspace / "earlier.txt").write_text(earlier_content)
    (workspace / "restored.txt").write_text("restored file bytes\n")
    config_path = workspace / ".voidcode.json"
    tui_config = {
        "keymap": {
            "ctrl+p": "app.history.search",
            "ctrl+r": "app.session.resume",
            "ctrl+t": "app.session.tree",
            "f1": "app.help",
            "f2": "app.tui.appearance",
            "f3": "app.tools.detail",
        },
        "theme": {"mode": "auto"},
    }
    config_path.write_text(json.dumps({"tui": tui_config}))

    def tool_response(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            finish="tool_calls",
        )

    provider = OpenAILoopbackServer(
        [
            tool_response("call-read-earlier", "read", {"path": "earlier.txt"}),
            openai_response(
                message={"role": "assistant", "content": "Read 1201 line(s) from earlier.txt."},
                finish="stop",
            ),
            tool_response("call-read-restored-artifact", "read", {"path": "restored.txt"}),
            openai_response(message={"role": "assistant", "content": "Read restored file."}, finish="stop"),
            tool_response("call-run-pwd-allow", "shell_exec", {"command": "pwd"}),
            openai_response(message={"role": "assistant", "content": "Ran pwd."}, finish="stop"),
            tool_response("call-run-pwd-deny-40", "shell_exec", {"command": "pwd"}),
            openai_response(message={"role": "assistant", "content": "The command was denied."}, finish="stop"),
            tool_response("call-run-pwd-deny-20", "shell_exec", {"command": "pwd"}),
            openai_response(message={"role": "assistant", "content": "The command was denied."}, finish="stop"),
            tool_response("call-run-pwd-allow-20", "shell_exec", {"command": "pwd"}),
            openai_response(message={"role": "assistant", "content": "Ran pwd."}, finish="stop"),
            tool_response("call-run-pwd-deny-hold", "shell_exec", {"command": "pwd"}),
            openai_response(message={"role": "assistant", "content": "The command was denied."}, finish="stop"),
            tool_response("call-read-restored-tui", "read", {"path": "restored.txt"}),
            openai_response(message={"role": "assistant", "content": "Read restored file."}, finish="stop"),
            tool_response("call-run-sleep", "shell_exec", {"command": "sleep 15"}),
        ]
    )
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    configure_openai_provider(workspace, f"http://127.0.0.1:{provider.server_address[1]}/v1")
    provider_config = json.loads(config_path.read_text())
    provider_config["tui"] = tui_config
    config_path.write_text(json.dumps(provider_config))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "loopback-key")
    project_root = Path(__file__).resolve().parents[3]
    env = {
        "DEEPSEEK_API_KEY": "loopback-key",
        "APPDATA": str(config_home),
        "HOME": str(home),
        "LOCALAPPDATA": str(local_app_data),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root / "src"),
        "TERM": "tmux-256color",
        "USERPROFILE": str(home),
        "VOIDCODE_APPROVAL_MODE": "ask",
        "VOIDCODE_DB_PATH": str(root / "voidcode.sqlite3"),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "NATIVE_WORKSPACE": str(workspace),
    }
    socket = root / "t"
    public_runtime_state: dict[str, object] = {}
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    monkeypatch.setenv("VOIDCODE_DB_PATH", env["VOIDCODE_DB_PATH"])

    session = "native-acceptance"
    command = (
        "env -i "
        + " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        + " "
        + shlex.quote(sys.executable)
        + " -m voidcode tui --workspace "
        + shlex.quote(str(workspace))
    )

    def private_pane_tty_state() -> dict[str, object]:
        import termios

        target = f"{session}:0.0"
        pane_tty, pane_pid, pane_size = (
            _tmux(
                socket,
                "display-message",
                "-p",
                "-t",
                target,
                "#{pane_tty}|#{pane_pid}|#{pane_width}x#{pane_height}",
                env=env,
            )
            .strip()
            .split("|")
        )
        fd = os.open(pane_tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            rows, columns = termios.tcgetwinsize(fd)
            pane_pgid = os.getpgid(int(pane_pid))
            foreground_pgid = int(
                subprocess.run(
                    ["ps", "-o", "tpgid=", "-p", pane_pid],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=env,
                ).stdout.strip()
            )
        finally:
            os.close(fd)
        return {
            "pane_tty": pane_tty,
            "pane_pid": int(pane_pid),
            "pane_pgid": pane_pgid,
            "foreground_pgid": foreground_pgid,
            "os_tty_size_rows_columns": (rows, columns),
            "tmux_pane_size": pane_size,
        }

    def wait_for_screen(
        text: str,
        *,
        present: bool = True,
        timeout: float = 10,
        changed_from: str | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            if (text in pane) is present and (changed_from is None or pane != changed_from):
                return pane
            time.sleep(0.1)
        scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
        pytest.fail(
            f"native screen did {'not show' if present else 'not hide'} {text!r}:\n"
            f"{pane}\nscrollback:\n{scrollback}\npublic runtime state:\n"
            f"{json.dumps(public_runtime_state, indent=2)}\n"
            f"private pane TTY state:\n{json.dumps(private_pane_tty_state(), indent=2)}"
        )

    def wait_for_scrollback(text: str, timeout: float = 10, *, after: str | None = None) -> str:
        deadline = time.monotonic() + timeout
        scrollback = ""
        while time.monotonic() < deadline:
            scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
            if after is None:
                if text in scrollback:
                    return scrollback
            else:
                marker = scrollback.rfind(after)
                rendered = scrollback[marker + len(after) :] if marker >= 0 else ""
                if "".join(text.split()) in "".join(rendered.split()):
                    return scrollback
            time.sleep(0.1)
        pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
        pytest.fail(
            f"native scrollback did not show {text!r}{f' after {after!r}' if after is not None else ''}:\n"
            f"{pane}\nscrollback:\n{scrollback}\npublic runtime state:\n"
            f"{json.dumps(public_runtime_state, indent=2)}"
        )

    def pane_cursor() -> tuple[int, int]:
        cursor = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{cursor_x},#{cursor_y}", env=env)
        coordinates = cursor.strip().split(",")
        return int(coordinates[0]), int(coordinates[1])

    def wait_for_cursor_move(before: tuple[int, int]) -> tuple[int, int]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            cursor = pane_cursor()
            if cursor[0] > before[0] and cursor[1] == before[1]:
                return cursor
            time.sleep(0.05)
        pytest.fail(f"native filter cursor did not move from {before!r}; got {cursor!r}")

    def wait_for_cursor_at(expected: tuple[int, int]) -> tuple[int, int]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            cursor = pane_cursor()
            if cursor == expected:
                return cursor
            time.sleep(0.05)
        pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
        scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
        pytest.fail(f"native cursor did not reach {expected!r}; got {cursor!r}; pane:\n{pane}\nscrollback:\n{scrollback}")

    def appearance_value(pane: str, field: str) -> str:
        row = next(line for line in pane.splitlines() if f"{field}:" in line)
        return row.split(f"{field}:", 1)[1].rsplit("│", 1)[0].strip()

    def cycle_appearance_field(field: str, down_count: int, options: tuple[str, ...], target: str) -> str:
        if down_count:
            before = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", *("Down" for _ in range(down_count)), env=env)
            wait_for_screen(f"{field}:", changed_from=before)
        for _ in options:
            pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            current = appearance_value(pane, field)
            if current == target:
                return pane
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Right", env=env)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
                updated = appearance_value(pane, field)
                if updated != current:
                    break
                time.sleep(0.05)
            else:
                scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
                pytest.fail(f"native appearance {field} stayed {current!r} after Right; target={target!r}; pane:\n{pane}\nscrollback:\n{scrollback}")
        pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
        scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
        pytest.fail(f"native appearance picker did not select {field}: {target}; pane:\n{pane}\nscrollback:\n{scrollback}")

    def preview_theme(mode: str) -> str:
        return cycle_appearance_field("theme.mode", 0, VALID_TUI_THEME_MODES, mode)

    try:
        provider_thread.start()
        _tmux(socket, "new-session", "-d", "-s", "keepalive", "sleep", "60", env=env)
        _tmux(socket, "set-option", "-g", "exit-empty", "off", env=env)
        _tmux(socket, "set-option", "-g", "remain-on-exit", "on", env=env)
        _tmux(socket, "new-session", "-d", "-x", "80", "-y", "24", "-s", session, command, env=env)
        _tmux(socket, "kill-session", "-t", "keepalive", env=env)
        _tmux(socket, "set-option", "-g", "status", "off", env=env)
        pane = wait_for_screen("VOIDCODE", timeout=30)
        assert any(line.strip() == "◇V  VOIDCODE" for line in pane.splitlines())
        assert "Resumed Session" not in pane
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F1", env=env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            if "ask" in pane and "Theme source" in pane:
                break
            time.sleep(0.1)
        else:
            pytest.fail("native F1 help omitted the approval mode or auto-theme source")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("Theme source", present=False)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
        appearance = wait_for_screen("theme.mode: auto")
        assert "theme.mode: auto" in appearance
        appearance = preview_theme("dark")
        assert "theme.mode: dark" in appearance
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("╭─ Appearance", present=False)

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
        appearance = wait_for_screen("theme.mode: auto")
        assert "theme.mode: auto" in appearance
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("╭─ Appearance", present=False)

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
        wait_for_screen("theme.mode: auto")
        appearance = preview_theme("dark")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F1", env=env)
        help_panel = wait_for_screen("Theme: dark")
        assert "Theme: dark" in help_panel
        assert json.loads(config_path.read_text())["tui"]["theme"]["mode"] == "auto"
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("VoidCode Help", present=False)

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
        wait_for_screen("theme.mode: dark")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "s", env=env)
        wait_for_screen("Save appearance to:")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Right", "Enter", env=env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            saved = json.loads(config_path.read_text())
            if saved.get("tui", {}).get("theme", {}).get("mode") == "dark":
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"native workspace appearance save did not persist: {config_path.read_text()}")
        assert saved["tui"]["keymap"] == {
            "ctrl+p": "app.history.search",
            "ctrl+r": "app.session.resume",
            "ctrl+t": "app.session.tree",
            "f1": "app.help",
            "f2": "app.tui.appearance",
            "f3": "app.tools.detail",
        }
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F1", env=env)
        help_panel = wait_for_screen("Theme: dark")
        assert "Theme: dark" in help_panel
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("Theme: dark", present=False)

        prompt_text = "  read earlier.txt  "
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", prompt_text, env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)

        from voidcode.core.tool_context import ArtifactMissing, ArtifactPage, NextPage
        from voidcode.runtime.config.loading import load_runtime_config
        from voidcode.runtime.contracts import RuntimeRequest
        from voidcode.runtime.service import VoidCodeRuntime

        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                for item in runtime.list_sessions():
                    replay = runtime.replay_session(session_id=item.session.id)
                    requests = [event for event in replay.events if event.event_type == "runtime.request_received"]
                    if (
                        str(item.status) == "completed"
                        and requests
                        and requests[-1].payload["prompt"] == prompt_text
                        and any(
                            event.event_type in {"runtime.tool_completed", "runtime.failed"} and event.sequence > requests[-1].sequence
                            for event in replay.events
                        )
                    ):
                        break
                else:
                    time.sleep(0.05)
                    continue
                break
            else:
                pytest.fail("native TUI did not complete the initial resize-test prompt")

        # The TUI commits settled transcript rows only after the runtime iterator closes with a terminal session status.
        wait_for_scrollback("Read 1201 line(s) from earlier.txt.", timeout=30)

        status_profiles = statusline_profiles()
        cursor_shapes = tuple(shape for shape in VALID_TUI_COMPOSER_CURSOR_SHAPES if shape != "terminal")
        assert len(VALID_TUI_COMPOSER_STYLES) == 8
        assert len(status_profiles) == 7
        assert len(cursor_shapes) == 3
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
        baseline_panel = wait_for_screen("theme.mode: dark")
        baseline = {field: appearance_value(baseline_panel, field) for field in ("statusline.layout", "composer.style", "composer.cursor")}
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("╭─ Appearance", present=False)

        draft = "DRAFT-20x6  END"
        draft_active = True

        def resize_native(width: int, height: int) -> None:
            _tmux(socket, "resize-window", "-t", session, "-x", str(width), "-y", str(height), env=env)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                size = _tmux(
                    socket,
                    "display-message",
                    "-p",
                    "-t",
                    f"{session}:0.0",
                    "#{pane_width}x#{pane_height}",
                    env=env,
                ).strip()
                if size == f"{width}x{height}":
                    return
                time.sleep(0.05)
            pytest.fail(f"native pane did not reach {width}x{height}")

        def draft_cursor_cell(pane: str, offset: int) -> tuple[int, int] | None:
            consumed = 0
            for row, line in enumerate(pane.splitlines()):
                for length in range(len(draft) - consumed, 0, -1):
                    segment = draft[consumed : consumed + length]
                    start = line.find(segment)
                    if start < 0:
                        continue
                    end = consumed + length
                    if consumed <= offset < end or offset == end == len(draft):
                        column = visible_width(line[:start]) + visible_width(segment[: offset - consumed])
                        return min(19, column), row
                    consumed = end
                    break
            return None

        def wait_for_draft_caret(
            offset: int,
            *,
            width: int = 20,
            height: int = 6,
        ) -> tuple[str, tuple[int, int]]:
            deadline = time.monotonic() + 5
            pane = ""
            cell = None
            cursor = (0, 0)
            while time.monotonic() < deadline:
                pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
                cell = draft_cursor_cell(pane, offset)
                cursor = pane_cursor()
                if cell is not None and cursor == cell and 0 <= cursor[0] < width and 0 <= cursor[1] < height:
                    return pane, cell
                time.sleep(0.05)
            pytest.fail(f"native {width}x{height} caret did not track draft offset {offset}; got {cursor!r}, cell {cell!r}:\n{pane}")

        def check_narrow_appearance() -> None:
            resize_native(20, 6)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Left", env=env)
            left_pane, _ = wait_for_draft_caret(len(draft) - 1)
            assert draft in left_pane, f"native 20x6 raw draft changed after Left:\n{left_pane}"
            assert all(visible_width(line) <= 20 for line in left_pane.splitlines())
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Right", env=env)
            right_pane, _ = wait_for_draft_caret(len(draft))
            assert draft in right_pane, f"native 20x6 raw draft changed after Right:\n{right_pane}"
            assert all(visible_width(line) <= 20 for line in right_pane.splitlines())
            resize_native(40, 12)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Left", env=env)
            medium_pane, _ = wait_for_draft_caret(len(draft) - 1, width=40, height=12)
            assert draft in medium_pane, f"native 40x12 raw draft changed after Left:\n{medium_pane}"
            assert all(visible_width(line) <= 40 for line in medium_pane.splitlines())
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Right", env=env)
            medium_pane, _ = wait_for_draft_caret(len(draft), width=40, height=12)
            assert draft in medium_pane, f"native 40x12 raw draft changed after Right:\n{medium_pane}"
            assert all(visible_width(line) <= 40 for line in medium_pane.splitlines())
            resize_native(80, 24)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Left", env=env)
            wide_pane, _ = wait_for_draft_caret(
                len(draft) - 1,
                width=80,
                height=24,
            )
            assert draft in wide_pane, f"native 80x24 raw draft changed after Left:\n{wide_pane}"
            assert all(visible_width(line) <= 80 for line in wide_pane.splitlines())
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Right", env=env)
            wide_pane, _ = wait_for_draft_caret(
                len(draft),
                width=80,
                height=24,
            )
            assert draft in wide_pane, f"native 80x24 raw draft changed after Right:\n{wide_pane}"
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
            wide_appearance = wait_for_screen("theme.mode: dark")
            assert all(visible_width(line) <= 80 for line in wide_appearance.splitlines())
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
            wait_for_screen("╭─ Appearance", present=False)

        def apply_appearance(statusline: str, style: str, cursor_shape: str) -> None:
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
            wait_for_screen("theme.mode: dark")
            cycle_appearance_field("statusline.layout", 3, status_profiles, statusline)
            cycle_appearance_field("composer.style", 1, VALID_TUI_COMPOSER_STYLES, style)
            cycle_appearance_field("composer.cursor", 1, VALID_TUI_COMPOSER_CURSOR_SHAPES, cursor_shape)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
            wait_for_screen("╭─ Appearance", present=False)
            screen = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            if draft_active:
                assert draft in screen
            assert all(visible_width(line) <= 80 for line in screen.splitlines())

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", draft, env=env)

        assert draft in wait_for_screen(draft)

        for statusline in status_profiles:
            apply_appearance(statusline, baseline["composer.style"], baseline["composer.cursor"])
            check_narrow_appearance()
        for style in VALID_TUI_COMPOSER_STYLES:
            apply_appearance(baseline["statusline.layout"], style, baseline["composer.cursor"])
            check_narrow_appearance()
        config_before_cursor_preview = config_path.read_bytes()
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F2", env=env)
        appearance = wait_for_screen("theme.mode: dark")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", *("Down" for _ in range(5)), env=env)
        cursor_field = wait_for_screen("composer.cursor:", changed_from=appearance)
        assert appearance_value(cursor_field, "composer.cursor") == baseline["composer.cursor"]
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Right", env=env)
        rejected_cursor_preview = wait_for_screen("Preview rolled back: terminal did not report its cursor style")
        assert appearance_value(rejected_cursor_preview, "composer.cursor") == baseline["composer.cursor"]
        assert config_path.read_bytes() == config_before_cursor_preview
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("╭─ Appearance", present=False)

        combined = (
            next(value for value in status_profiles if value != baseline["statusline.layout"]),
            next(value for value in VALID_TUI_COMPOSER_STYLES if value != baseline["composer.style"]),
            baseline["composer.cursor"],
        )
        apply_appearance(*combined)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", *("BSpace" for _ in draft), env=env)
        wait_for_screen(draft, present=False)
        draft_active = False

        def wait_for_completed_prompt(
            target_session_id: str,
            expected_prompt: str,
            previous_request_count: int,
        ) -> dict[str, object]:
            with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    summary = next(
                        (item for item in runtime.list_sessions() if item.session.id == target_session_id),
                        None,
                    )
                    replay = runtime.replay_session(session_id=target_session_id)
                    requests = [event for event in replay.events if event.event_type == "runtime.request_received"]
                    completed = (
                        next(
                            (
                                event
                                for event in replay.events
                                if event.event_type == "runtime.tool_completed" and event.sequence > requests[-1].sequence
                            ),
                            None,
                        )
                        if len(requests) > previous_request_count
                        else None
                    )
                    if (
                        summary is not None
                        and str(summary.status) == "completed"
                        and len(requests) > previous_request_count
                        and requests[-1].payload["prompt"] == expected_prompt
                        and completed is not None
                    ):
                        return completed.payload
                    time.sleep(0.05)
            pytest.fail(
                f"native prompt {expected_prompt!r} did not complete in the public runtime; "
                f"requests={[(event.payload.get('prompt'), event.sequence) for event in requests]!r}; "
                f"status={str(summary.status) if summary is not None else 'missing'}"
            )

        def wait_for_denied_prompt(
            target_session_id: str,
            expected_prompt: str,
            previous_request_count: int,
        ) -> tuple[int, ...]:
            with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    summary = next(
                        (item for item in runtime.list_sessions() if item.session.id == target_session_id),
                        None,
                    )
                    replay = runtime.replay_session(session_id=target_session_id)
                    requests = [event for event in replay.events if event.event_type == "runtime.request_received"]
                    if len(requests) > previous_request_count and requests[-1].payload["prompt"] == expected_prompt:
                        sequence = requests[-1].sequence
                        after_request = [event for event in replay.events if event.sequence > sequence]
                        resolved = next(
                            (event for event in after_request if event.event_type == "runtime.approval_resolved"),
                            None,
                        )
                        denied = next(
                            (
                                event
                                for event in after_request
                                if event.event_type == "runtime.tool_completed" and event.payload.get("permission_denied") is True
                            ),
                            None,
                        )
                        response_ready = next(
                            (event for event in after_request if event.event_type == "core.response_ready"),
                            None,
                        )
                        if (
                            summary is not None
                            and str(summary.status) == "completed"
                            and resolved is not None
                            and resolved.payload["decision"] == "deny"
                            and denied is not None
                            and denied.sequence > resolved.sequence
                            and response_ready is not None
                            and response_ready.sequence > denied.sequence
                            and not any(
                                event.event_type == "runtime.approval_requested" and event.sequence > resolved.sequence for event in replay.events
                            )
                        ):
                            return tuple(event.sequence for event in after_request)
                    time.sleep(0.05)
            pytest.fail(
                f"native denial did not reach a terminal response for {expected_prompt!r}; "
                f"status={str(summary.status) if summary is not None else 'missing'}; "
                f"events={[(event.sequence, event.event_type) for event in replay.events]!r}"
            )

        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                summaries = runtime.list_sessions()
                if summaries:
                    summary = summaries[0]
                    replay = runtime.replay_session(session_id=summary.session.id)
                    event_types = [event.event_type for event in replay.events]
                    if str(summary.status) == "completed" and "runtime.tool_completed" in event_types and "core.response_ready" in event_types:
                        break
                time.sleep(0.1)
            else:
                pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
                scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
                public_state = {
                    "session_id": summary.session.id if summaries else None,
                    "status": str(summary.status) if summaries else None,
                    "event_types": event_types if summaries else [],
                    "output_preview": (replay.output or "")[:500] if summaries else "",
                }
                pytest.fail(
                    "native deterministic prompt did not complete:\n"
                    f"{pane}\nscrollback:\n{scrollback}\npublic runtime state:\n"
                    f"{json.dumps(public_state, indent=2)}"
                )

            session_id = summary.session.id
            request = next(event for event in replay.events if event.event_type == "runtime.request_received")
            previous_request_count = sum(event.event_type == "runtime.request_received" for event in replay.events)
            request_prompt = request.payload["prompt"]
            assert isinstance(request_prompt, str)
            assert request_prompt == prompt_text
            assert "◇V" not in request_prompt
            live_panel = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            assert not any(line.strip() == "◇V  VOIDCODE" for line in live_panel.splitlines())
            public_runtime_state.update(
                {
                    "session_id": session_id,
                    "status": str(summary.status),
                    "event_types": event_types,
                    "output_preview": (replay.output or "")[:500],
                    "request_prompt": request.payload["prompt"],
                }
            )
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-p", env=env)
            history = wait_for_screen("Search Prompt History")
            assert "read earlier.txt" in history
            history_cursor_before = pane_cursor()
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "earlier", env=env)
            assert "earlier" in wait_for_screen("earlier")
            history_cursor_after = wait_for_cursor_move(history_cursor_before)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
            wait_for_screen("Search Prompt History", present=False)

            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-t", env=env)
            tree = wait_for_screen("Session Entries")
            assert "read earlier.txt" in tree
            tree_cursor_before = pane_cursor()
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "earlier", env=env)
            assert "earlier" in wait_for_screen("earlier")
            tree_cursor_after = wait_for_cursor_move(tree_cursor_before)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
            wait_for_screen("Session Entries", present=False)

            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F3", env=env)
            picker = wait_for_screen("Select Tool Output")
            assert "earlier.txt" in picker
            picker_cursor_before = pane_cursor()
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "earlier", env=env)
            picker = wait_for_screen("earlier")
            picker_cursor_after = wait_for_cursor_move(picker_cursor_before)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
            wait_for_screen("Select Tool Output", present=False)
            completed = next(event for event in replay.events if event.event_type == "runtime.tool_completed")
            artifact = completed.payload.get("artifact")
            artifact_id = completed.payload.get("artifact_id")
            if not isinstance(artifact_id, str) and isinstance(artifact, dict):
                artifact_id = artifact.get("artifact_id")
            tool_call_id = completed.payload.get("tool_call_id")
            assert isinstance(artifact_id, str) and isinstance(tool_call_id, str)
            artifact_info = runtime.resolve_tool_output_artifact(
                session_id=session_id,
                artifact_id=artifact_id,
                tool_call_id=tool_call_id,
            )
            assert artifact_info["status"] == "available", artifact_info
            line_count = artifact_info["line_count"]
            assert isinstance(line_count, int) and line_count >= 1200
            pane_height = int(_tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_height}", env=env).strip())
            page_limit = max(1, pane_height - 5)
            artifact_pages = []
            artifact_offsets = [0]
            offset = 0
            while True:
                page = runtime.read_tool_output_artifact(
                    caller_session_id=session_id,
                    session_id=session_id,
                    artifact_id=artifact_id,
                    tool_call_id=tool_call_id,
                    offset=offset,
                    limit=page_limit,
                )
                assert isinstance(page, ArtifactPage)
                artifact_pages.append(page)
                if not isinstance(page.page, NextPage):
                    break
                assert page.page.offset > offset
                artifact_offsets.append(page.page.offset)
                offset = page.page.offset
            if "NATIVE_PTY_READY" not in artifact_pages[-1].text:
                pytest.fail(
                    f"authorized public final page omitted marker; metadata={artifact_info!r}; "
                    f"final_page={artifact_pages[-1].text!r}; public state={public_runtime_state!r}"
                )

        native_prefix_before = _tmux(socket, "capture-pane", "-p", "-S", "-", "-E", "-2", "-t", f"{session}:0.0", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "F3", env=env)
        wait_for_screen("Select Tool Output")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        page_rows = []
        for index, page in enumerate(artifact_pages):
            if index:
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "PageDown", env=env)
            row = next(line.strip() for line in page.text.splitlines() if "EARLIER_ROW_" in line)
            page_rows.append(row)
            pane = wait_for_screen(row)
        assert len(page_rows) == len(artifact_pages)
        row_numbers = [int(row.rsplit("_", 1)[1]) for row in page_rows]
        assert row_numbers == sorted(set(row_numbers))
        if "NATIVE_PTY_READY" not in pane:
            last_pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
            pytest.fail(
                f"native authorized artifact page omitted NATIVE_PTY_READY:\n{last_pane}\n"
                f"scrollback:\n{scrollback}\npublic runtime state:\n"
                f"{json.dumps(public_runtime_state, indent=2)}"
            )
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        wait_for_screen("Select Tool Output", present=False)
        native_prefix_after = _tmux(socket, "capture-pane", "-p", "-S", "-", "-E", "-2", "-t", f"{session}:0.0", env=env)
        assert native_prefix_after.startswith(native_prefix_before)
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            unrelated = tuple(runtime.run_stream(RuntimeRequest(prompt="read restored.txt", allocate_session_id=True)))
            unrelated_session_id = unrelated[-1].session.session.id
            refused = runtime.read_tool_output_artifact(
                caller_session_id=unrelated_session_id,
                session_id=session_id,
                artifact_id=artifact_id,
                tool_call_id=tool_call_id,
            )
            assert isinstance(refused, ArtifactMissing)
            assert _tmux(socket, "capture-pane", "-p", "-S", "-", "-E", "-2", "-t", f"{session}:0.0", env=env) == native_prefix_after
            replay_after = runtime.replay_session(session_id=session_id)
            assert replay_after.events == replay.events

        print(
            json.dumps(
                {
                    "appearance": {"tmux_cursor_preview_refused": True},
                    "artifact_byte_count": artifact_info["byte_count"],
                    "artifact_line_count": artifact_info["line_count"],
                    "artifact_page_offsets": artifact_offsets,
                    "artifact_sha256": artifact_info["sha256"],
                    "caller_refusal": type(refused).__name__,
                    "cursor_coordinates": {
                        "history": [history_cursor_before, history_cursor_after],
                        "session_picker": [picker_cursor_before, picker_cursor_after],
                        "session_tree": [tree_cursor_before, tree_cursor_after],
                    },
                    "final_page_marker": "NATIVE_PTY_READY",
                    "final_screen": pane,
                    "native_prefix_sha256": hashlib.sha256(native_prefix_after.encode()).hexdigest(),
                    "public_output_unchanged": replay_after.output == replay.output,
                    "public_replay_unchanged": True,
                    "screen_history_filter": history,
                    "screen_session_tree_filter": tree,
                },
                sort_keys=True,
            )
        )
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "run pwd", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        approval = wait_for_screen("Allow", timeout=30)
        assert "shell_exec" in approval and "pwd" in approval and "Deny" in approval
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "y", env=env)
        completed = wait_for_completed_prompt(session_id, "run pwd", previous_request_count)
        assert completed["status"] == "ok"
        assert completed["content"] == f"{workspace}\n"
        wait_for_scrollback(str(workspace), timeout=30, after="run pwd")
        previous_request_count += 1

        def pending_state() -> tuple[object, ...]:
            with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
                summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                replay = runtime.replay_session(session_id=session_id)
                requests = tuple(
                    (event.sequence, event.payload["prompt"]) for event in replay.events if event.event_type == "runtime.request_received"
                )
                messages = replay.session.metadata.get("pending_messages", [])
                message_ids = (
                    tuple(message["id"] for message in messages if isinstance(message, dict) and isinstance(message.get("id"), str))
                    if isinstance(messages, list)
                    else ()
                )
                return (
                    replay.events,
                    summary.prompt,
                    str(summary.status),
                    requests,
                    message_ids,
                )

        pending_approval = False

        for width, height in ((40, 12), (40, 8), (20, 6), (10, 5), (80, 24)):
            _tmux(socket, "resize-window", "-t", session, "-x", str(width), "-y", str(height), env=env)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                size = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_width}x#{pane_height}", env=env)
                if size.strip() == f"{width}x{height}":
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"native pane did not reach requested size {width}x{height}")
            dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env)
            assert dead.strip() == "0"
            if (width, height) == (40, 12):
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "run pwd", env=env)
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
                approval = wait_for_screen("y Allow", timeout=30)
                assert all(value in approval for value in ("n Deny", "Target:", "shell_exec", "Why:", "pwd"))
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "n", env=env)
                wait_for_screen("y Allow", present=False)
                denied_40_sequences = wait_for_denied_prompt(session_id, "run pwd", previous_request_count)
                previous_request_count += 1
            elif (width, height) == (20, 6):
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "run pwd", env=env)
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
                approval = wait_for_screen("y Allow", timeout=30)
                approval_views = [approval]
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "PageDown", env=env)
                approval_page = wait_for_screen("y Allow", changed_from=approval)
                assert "y Allow" in approval_page and "n Deny" in approval_page
                approval_views.append(approval_page)
                for _ in range(12):
                    joined = "\n".join(approval_views)
                    if all(value in joined for value in ("Target:", "shell_exec", "Why:", "pwd")):
                        break
                    _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Down", env=env)
                    detail_page = wait_for_screen("y Allow", changed_from=approval_views[-1])
                    assert "y Allow" in detail_page and "n Deny" in detail_page
                    approval_views.append(detail_page)
                else:
                    pytest.fail(f"20x6 approval detail scroll omitted target/reason/arguments:\n{approval_views!r}")
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "n", env=env)
                wait_for_screen("y Allow", present=False)
                denied_20_sequences = wait_for_denied_prompt(session_id, "run pwd", previous_request_count)
                previous_request_count += 1
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "run pwd", env=env)
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
                approval = wait_for_screen("y Allow", timeout=30)
                assert "n Deny" in approval and "Target:" in approval and "Why:" in approval
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "y", env=env)
                wait_for_screen("y Allow", present=False)
                wait_for_completed_prompt(session_id, "run pwd", previous_request_count)
                previous_request_count += 1
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "run pwd", env=env)
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
                approval = wait_for_screen("y Allow", timeout=30)
                assert "n Deny" in approval and "Target:" in approval and "Why:" in approval
                pending_approval = True
            elif (width, height) == (10, 5):
                assert pending_approval
                hold_screen_before = wait_for_screen("Resize to")
                pending_before = pending_state()
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "ZZZ", env=env)
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
                hold_screen_after = wait_for_screen("Resize to")
                assert hold_screen_after == hold_screen_before
                assert pending_state() == pending_before
                assert "Resize to" in wait_for_screen("Resize to")
            elif (width, height) == (80, 24) and pending_approval:
                approval = wait_for_screen("Allow")
                assert "shell_exec" in approval and "pwd" in approval and "Deny" in approval
                _tmux(socket, "send-keys", "-t", f"{session}:0.0", "n", env=env)
                wait_for_screen("Allow", present=False)
                denied_after_hold_sequences = wait_for_denied_prompt(session_id, "run pwd", previous_request_count)
                previous_request_count += 1
                pending_approval = False

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "read restored.txt", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        restored_result = wait_for_completed_prompt(session_id, "read restored.txt", previous_request_count)
        previous_request_count += 1
        assert "restored file bytes" in str(restored_result.get("content", ""))
        wait_for_scrollback("Read · restored.txt")
        apply_appearance("minimal", baseline["composer.style"], baseline["composer.cursor"])
        resize_native(40, 12)

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "run sleep 15", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        approval = wait_for_screen("Allow", timeout=30)
        assert "shell_exec" in approval and "sleep 15" in approval and "Waiting approval" in approval
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "y", env=env)
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                active_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                active_replay = runtime.replay_session(session_id=session_id)
                active_event_types = [event.event_type for event in active_replay.events]
                latest_request = next(event.sequence for event in reversed(active_replay.events) if event.event_type == "runtime.request_received")
                tool_started = any(event.event_type == "runtime.tool_started" and event.sequence > latest_request for event in active_replay.events)
                if str(active_summary.status) in {"running", "waiting"} and tool_started:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(
                    f"native approved shell run did not start: "
                    f"{json.dumps({'status': str(active_summary.status), 'event_types': active_event_types})}"
                )
            public_runtime_state.update(
                {
                    "session_id": session_id,
                    "status": str(active_summary.status),
                    "event_types": active_event_types,
                    "output_preview": (active_replay.output or "")[:500],
                }
            )
        running_screen = wait_for_screen("Running")
        assert "Running" in running_screen

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "continue", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        steering_screen = wait_for_screen("Steering queued (1)")
        assert "continue" in steering_screen and "Running" in steering_screen
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            queued_replay = runtime.replay_session(session_id=session_id)
            queued_messages = queued_replay.session.metadata.get("pending_messages", [])
            assert isinstance(queued_messages, list) and len(queued_messages) == 1
            assert queued_messages[0]["kind"] == "steering" and queued_messages[0]["content"] == "continue"
            steering_ids = tuple(message["id"] for message in queued_messages)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                cancelled_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                cancelled_replay = runtime.replay_session(session_id=session_id)
                cancelled_event = next(
                    (
                        event
                        for event in reversed(cancelled_replay.events)
                        if event.event_type == "runtime.failed" and event.payload.get("cancelled") is True
                    ),
                    None,
                )
                public_runtime_state.update(
                    {
                        "session_id": session_id,
                        "status": str(cancelled_summary.status),
                        "event_types": [event.event_type for event in cancelled_replay.events],
                        "output_preview": (cancelled_replay.output or "")[:500],
                    }
                )
                if str(cancelled_summary.status) == "interrupted" and cancelled_event is not None:
                    break
                time.sleep(0.05)
            else:
                pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
                scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
                pytest.fail(
                    "native Esc did not interrupt the approved run:\n"
                    f"{pane}\nscrollback:\n{scrollback}\npublic runtime state:\n"
                    f"{json.dumps(public_runtime_state, indent=2)}"
                )
            assert cancelled_event.payload["reason"] == "tui_turn_cancel"
            remaining_messages = cancelled_replay.session.metadata.get("pending_messages", [])
            assert isinstance(remaining_messages, list)
            assert tuple(message["id"] for message in remaining_messages) == steering_ids
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-c", "C-c", env=env)
        deadline = time.monotonic() + 10
        dead = "0"
        while time.monotonic() < deadline:
            dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env).strip()
            if dead == "1":
                break
            time.sleep(0.05)
        assert dead == "1", _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
        print(
            json.dumps(
                {
                    "native_terminal": "tmux",
                    "steering_notice": steering_screen,
                    "approval_denial_sequences": {
                        "at_40x12": denied_40_sequences,
                        "at_20x6": denied_20_sequences,
                        "after_hold": denied_after_hold_sequences,
                    },
                    "small_terminal_hold": {
                        "status": pending_before[2],
                        "requests": pending_before[3],
                        "pending_message_ids": pending_before[4],
                    },
                    "appearance": {"tmux_cursor_preview_refused": True},
                    "cancelled_runtime_event": cancelled_event.payload,
                    "session_status": str(cancelled_summary.status),
                }
            )
        )
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "-f", "/dev/null", "kill-server"],
            env=env,
            check=False,
            capture_output=True,
        )
        if provider_thread.is_alive():
            provider.shutdown()
            provider_thread.join(timeout=5)
        provider.server_close()


def test_native_tui_persists_active_steering_through_cancel_and_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voidcode.runtime.config.loading import load_runtime_config
    from voidcode.runtime.service import VoidCodeRuntime

    root = tmp_path
    workspace = root / "workspace"
    home = root / "home"
    config_home = root / "config"
    local_app_data = root / "localappdata"
    tmux_tmpdir = root / "tmux"
    for directory in (workspace, home, config_home, local_app_data, tmux_tmpdir):
        directory.mkdir()
    (workspace / "restored.txt").write_text("first re-entry file bytes\n")

    def tool_response(call_id: str, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        return openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            finish="tool_calls",
        )

    provider = OpenAILoopbackServer(
        [
            tool_response("call-steering-sleep", "shell_exec", {"command": "sleep 15"}),
            tool_response("call-steering-reentry-read", "read", {"path": "restored.txt"}),
            openai_response(message={"role": "assistant", "content": "Read after cancellation."}, finish="stop"),
            tool_response("call-steering-clean-read", "read", {"path": "restored.txt"}),
            openai_response(message={"role": "assistant", "content": "Read after clean re-entry."}, finish="stop"),
        ]
    )
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    configure_openai_provider(workspace, f"http://127.0.0.1:{provider.server_address[1]}/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "loopback-key")
    socket = root / "tmux.sock"
    session = "native-steering"
    database = root / "voidcode.sqlite3"
    env = {
        "DEEPSEEK_API_KEY": "loopback-key",
        "APPDATA": str(config_home),
        "HOME": str(home),
        "LOCALAPPDATA": str(local_app_data),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        "TERM": "tmux-256color",
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "USERPROFILE": str(home),
        "VOIDCODE_APPROVAL_MODE": "ask",
        "VOIDCODE_DB_PATH": str(database),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
    }
    for name in ("cache", "data", "state"):
        (root / name).mkdir()
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(database))

    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    terminal_before = root / "termios-before"
    terminal_after = root / "termios-after"
    app_command = (
        "env -i "
        + " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        + " "
        + shlex.quote(sys.executable)
        + " -m voidcode tui --workspace "
        + shlex.quote(str(workspace))
    )
    termios_prefix = f"stty -g > {shlex.quote(str(terminal_before))}; "
    termios_suffix = f"; status=$?; stty -g > {shlex.quote(str(terminal_after))}; exit $status"
    command = "sh -c " + shlex.quote(termios_prefix + app_command + termios_suffix)

    def capture() -> str:
        return _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)

    def wait_for_screen(text: str, *, present: bool = True, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = capture()
            if (text in pane) is present:
                return pane
            time.sleep(0.05)
        cursor = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{cursor_x},#{cursor_y}", env=env).strip()
        pane_dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env).strip()
        pytest.fail(f"native screen did not {'show' if present else 'hide'} {text!r}; pane_dead={pane_dead}; cursor={cursor}:\n{pane}")

    try:
        provider_thread.start()
        _tmux(socket, "new-session", "-d", "-s", "keepalive", "sleep", "60", env=env)
        _tmux(socket, "set-option", "-g", "exit-empty", "off", env=env)
        _tmux(socket, "set-option", "-g", "remain-on-exit", "on", env=env)
        _tmux(socket, "new-session", "-d", "-x", "80", "-y", "24", "-s", session, command, env=env)
        _tmux(socket, "kill-session", "-t", "keepalive", env=env)
        _tmux(socket, "set-option", "-g", "status", "off", env=env)
        wait_for_screen("VOIDCODE", timeout=30)

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "run sleep 15", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        approval = wait_for_screen("y Allow", timeout=30)
        assert "shell_exec" in approval and "sleep 15" in approval
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "y", env=env)

        session_id: str | None = None
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                summaries = runtime.list_sessions()
                if summaries:
                    summary = summaries[0]
                    session_id = summary.session.id
                    replay = runtime.replay_session(session_id=session_id)
                    requests = [event for event in replay.events if event.event_type == "runtime.request_received"]
                    started = (
                        any(event.event_type == "runtime.tool_started" and event.sequence > requests[-1].sequence for event in replay.events)
                        if requests
                        else False
                    )
                    if str(summary.status) in {"running", "waiting"} and started:
                        break
                time.sleep(0.05)
            else:
                pytest.fail("native approved sleep did not reach public tool_started state")
            assert session_id is not None

        wait_for_screen("Running")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "continue", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        steering_screen = wait_for_screen("Steering queued (1)")
        assert "continue" in steering_screen and "Running" in steering_screen
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            queued_replay = runtime.replay_session(session_id=session_id)
            pending = queued_replay.session.metadata.get("pending_messages", [])
            assert isinstance(pending, list) and len(pending) == 1
            assert pending[0]["kind"] == "steering" and pending[0]["content"] == "continue"
            steering_ids = tuple(message["id"] for message in pending)
            queued_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
            assert str(queued_summary.status) in {"running", "waiting"}
            assert any(event.event_type == "runtime.tool_started" for event in queued_replay.events)
            first_request = next(event for event in reversed(queued_replay.events) if event.event_type == "runtime.request_received")
            active_tool_started = next(
                event for event in queued_replay.events if event.event_type == "runtime.tool_started" and event.sequence > first_request.sequence
            )

        escape_after_sequence = queued_replay.events[-1].sequence
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                cancelled_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                cancelled_replay = runtime.replay_session(session_id=session_id)
                cancelled = next(
                    (
                        event
                        for event in reversed(cancelled_replay.events)
                        if event.event_type == "runtime.failed" and event.payload.get("cancelled") is True
                    ),
                    None,
                )
                if str(cancelled_summary.status) == "interrupted" and cancelled is not None:
                    break
                time.sleep(0.05)
            else:
                pane = capture()
                scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
                cursor = _tmux(
                    socket,
                    "display-message",
                    "-p",
                    "-t",
                    f"{session}:0.0",
                    "#{cursor_x},#{cursor_y}",
                    env=env,
                ).strip()
                pane_dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env).strip()
                events_after_escape = [
                    {
                        "sequence": event.sequence,
                        "event_type": event.event_type,
                        "payload": event.payload,
                    }
                    for event in cancelled_replay.events
                    if event.sequence > escape_after_sequence
                ]
                pending_after = cancelled_replay.session.metadata.get("pending_messages", [])
                tool_lifecycle = [
                    {"sequence": event.sequence, "event_type": event.event_type}
                    for event in cancelled_replay.events
                    if event.event_type in {"runtime.tool_started", "runtime.tool_completed"}
                ]
                pytest.fail(
                    "native Escape did not interrupt the active approved sleep:\n"
                    f"session_status={cancelled_summary.status}; pane_dead={pane_dead}; cursor={cursor}\n"
                    f"pane:\n{pane}\nscrollback:\n{scrollback}\n"
                    f"public_events_after_escape={json.dumps(events_after_escape, indent=2, sort_keys=True)}\n"
                    f"pending_messages={json.dumps(pending_after, indent=2, sort_keys=True)}\n"
                    f"tool_lifecycle={json.dumps(tool_lifecycle, indent=2)}"
                )
            assert cancelled.payload["reason"] == "tui_turn_cancel"
            cancel_sequence = cancelled.sequence
            remaining = cancelled_replay.session.metadata.get("pending_messages", [])
            assert isinstance(remaining, list)
            assert tuple(message["id"] for message in remaining) == steering_ids
            tool_lifecycle = [event for event in cancelled_replay.events if event.event_type in {"runtime.tool_started", "runtime.tool_completed"}]
            assert [event.event_type for event in tool_lifecycle] == ["runtime.tool_started", "runtime.tool_completed"], tool_lifecycle
            assert tool_lifecycle[0].sequence == active_tool_started.sequence
            assert tool_lifecycle[1].sequence < cancelled.sequence
        # DB interruption can precede the app pump; wait until neither active nor failed status remains.
        wait_for_screen("Running", present=False)
        wait_for_screen("Failed ·", present=False)
        wait_for_screen("Interrupted")

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "read restored.txt", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        expected_prompt = "read restored.txt\n\nRuntime steering messages:\ncontinue"
        reentry_request = None
        reentry_response = None
        reentry_tool_events = []
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                reentry_replay = runtime.replay_session(session_id=session_id)
                reentry_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                reentry_request = next(
                    (
                        event
                        for event in reversed(reentry_replay.events)
                        if event.event_type == "runtime.request_received" and event.sequence > cancel_sequence
                    ),
                    None,
                )
                if reentry_request is not None:
                    reentry_tool_events = [
                        event
                        for event in reentry_replay.events
                        if event.sequence > reentry_request.sequence and event.event_type in {"runtime.tool_started", "runtime.tool_completed"}
                    ]
                    reentry_response = next(
                        (
                            event
                            for event in reentry_replay.events
                            if event.event_type == "core.response_ready" and event.sequence > reentry_request.sequence
                        ),
                        None,
                    )
                    if len(reentry_tool_events) == 2 and reentry_response is not None and str(reentry_summary.status) == "completed":
                        break
                time.sleep(0.05)
            else:
                pane = capture()
                scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
                cursor = _tmux(
                    socket,
                    "display-message",
                    "-p",
                    "-t",
                    f"{session}:0.0",
                    "#{cursor_x},#{cursor_y}",
                    env=env,
                ).strip()
                pane_dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env).strip()
                events_after_cancel = [
                    {"sequence": event.sequence, "event_type": event.event_type, "payload": event.payload}
                    for event in reentry_replay.events
                    if event.sequence > cancel_sequence
                ]
                pending_after_reentry = reentry_replay.session.metadata.get("pending_messages", [])
                pytest.fail(
                    "native TUI did not persist the queued-steering reentry result:\n"
                    f"session_status={reentry_summary.status}; pane_dead={pane_dead}; cursor={cursor}\n"
                    f"pane:\n{pane}\nscrollback:\n{scrollback}\n"
                    f"public_events_after_cancel={json.dumps(events_after_cancel, indent=2, sort_keys=True)}\n"
                    f"pending_messages={json.dumps(pending_after_reentry, indent=2, sort_keys=True)}"
                )
        assert reentry_request is not None and reentry_response is not None
        assert reentry_request.payload["prompt"] == expected_prompt
        assert "pending_messages" not in reentry_replay.session.metadata
        assert [event.event_type for event in reentry_tool_events] == ["runtime.tool_started", "runtime.tool_completed"], reentry_tool_events
        assert reentry_tool_events[-1].payload["tool"] == "read"
        read_output = reentry_tool_events[-1].payload.get("content")
        assert isinstance(read_output, str) and "first re-entry file bytes" in read_output
        assert "Read after cancellation." in str(reentry_response.payload.get("output_preview", ""))
        assert str(reentry_summary.status) == "completed"
        wait_for_screen("Read after cancellation.")

        (workspace / "restored.txt").write_text("clean re-entry file bytes\n")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "read restored.txt", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            clean_tool_events = []
            clean_request = None
            clean_response = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                clean_replay = runtime.replay_session(session_id=session_id)
                clean_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                clean_request = next(
                    (
                        event
                        for event in reversed(clean_replay.events)
                        if event.event_type == "runtime.request_received" and event.sequence > reentry_response.sequence
                    ),
                    None,
                )
                if clean_request is not None:
                    clean_tool_events = [
                        event
                        for event in clean_replay.events
                        if event.sequence > clean_request.sequence and event.event_type in {"runtime.tool_started", "runtime.tool_completed"}
                    ]
                    clean_response = next(
                        (
                            event
                            for event in clean_replay.events
                            if event.event_type == "core.response_ready" and event.sequence > clean_request.sequence
                        ),
                        None,
                    )
                    if len(clean_tool_events) == 2 and clean_response is not None and str(clean_summary.status) == "completed":
                        break
                time.sleep(0.05)
            else:
                pane = capture()
                scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
                cursor = _tmux(
                    socket,
                    "display-message",
                    "-p",
                    "-t",
                    f"{session}:0.0",
                    "#{cursor_x},#{cursor_y}",
                    env=env,
                ).strip()
                pane_dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env).strip()
                events_after_reentry = [
                    {"sequence": event.sequence, "event_type": event.event_type, "payload": event.payload}
                    for event in clean_replay.events
                    if event.sequence > reentry_response.sequence
                ]
                pytest.fail(
                    "native TUI did not complete the clean post-queue read:\n"
                    f"session_status={clean_summary.status}; pane_dead={pane_dead}; cursor={cursor}\n"
                    f"pane:\n{pane}\nscrollback:\n{scrollback}\n"
                    f"public_events_after_queued_reentry={json.dumps(events_after_reentry, indent=2, sort_keys=True)}"
                )
        assert clean_request is not None and clean_response is not None
        assert clean_request.payload["prompt"] == "read restored.txt"
        assert "pending_messages" not in clean_replay.session.metadata
        assert [event.event_type for event in clean_tool_events] == ["runtime.tool_started", "runtime.tool_completed"], clean_tool_events
        assert clean_tool_events[-1].payload["tool"] == "read"
        clean_output = clean_tool_events[-1].payload.get("content")
        assert isinstance(clean_output, str) and "clean re-entry file bytes" in clean_output
        assert "Read after clean re-entry." in str(clean_response.payload.get("output_preview", ""))
        assert not any(event.event_type == "runtime.approval_requested" and event.sequence > clean_request.sequence for event in clean_replay.events)
        assert str(clean_summary.status) == "completed"
        wait_for_screen("Read after clean re-entry.")

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-c", "C-c", env=env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env).strip()
            if dead == "1":
                break
            time.sleep(0.05)
        assert dead == "1"
        assert terminal_before.read_text().strip() == terminal_after.read_text().strip()
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "-f", "/dev/null", "kill-server"],
            env=env,
            check=False,
            capture_output=True,
        )
        if provider_thread.is_alive():
            provider.shutdown()
            provider_thread.join(timeout=5)
        provider.server_close()


def test_native_tui_provider_read_cancel_and_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voidcode.runtime.config.loading import load_runtime_config
    from voidcode.runtime.service import VoidCodeRuntime

    root = tmp_path
    workspace = root / "workspace"
    home = root / "home"
    config_home = root / "config"
    local_app_data = root / "localappdata"
    tmux_tmpdir = root / "tmux"
    for directory in (workspace, home, config_home, local_app_data, tmux_tmpdir):
        directory.mkdir()
    (workspace / "restored.txt").write_text("POST_CANCEL_PROVIDER_READ\n")

    def tool_response(
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        return openai_response(
            message={
                "role": "assistant",
                "content": None,
                "reasoning_content": "loopback reasoning",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            },
            finish="tool_calls",
        )

    provider = OpenAILoopbackServer(
        [
            tool_response(
                "call-shell-sleep",
                "shell_exec",
                {"command": "sleep 15"},
            ),
            tool_response(
                "call-read-after-cancel",
                "read",
                {"path": "restored.txt"},
            ),
            openai_response(
                message={"role": "assistant", "content": "Read after cancellation."},
                finish="stop",
            ),
            tool_response(
                "call-read-clean-reentry",
                "read",
                {"path": "restored.txt"},
            ),
            openai_response(
                message={"role": "assistant", "content": "Read after clean re-entry."},
                finish="stop",
            ),
        ]
    )
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    configure_openai_provider(
        workspace,
        f"http://127.0.0.1:{provider.server_address[1]}/v1",
    )

    state_home = root / "state"
    cache_home = root / "cache"
    data_home = root / "data"
    for directory in (state_home, cache_home, data_home):
        directory.mkdir()
    database = root / "voidcode.sqlite3"
    env = {
        "APPDATA": str(config_home),
        "DEEPSEEK_API_KEY": "loopback-key",
        "HOME": str(home),
        "LOCALAPPDATA": str(local_app_data),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        "TERM": "tmux-256color",
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "USERPROFILE": str(home),
        "VOIDCODE_APPROVAL_MODE": "ask",
        "VOIDCODE_DB_PATH": str(database),
        "XDG_CACHE_HOME": str(cache_home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    monkeypatch.setenv("DEEPSEEK_API_KEY", env["DEEPSEEK_API_KEY"])
    monkeypatch.setenv("VOIDCODE_DB_PATH", env["VOIDCODE_DB_PATH"])
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    socket = root / "tmux.sock"
    session = "native-provider-cancel"
    terminal_before = root / "termios-before"
    terminal_after = root / "termios-after"
    app_command = (
        "env -i "
        + " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        + " "
        + shlex.quote(sys.executable)
        + " -m voidcode tui --workspace "
        + shlex.quote(str(workspace))
    )
    command = "sh -c " + shlex.quote(
        f"stty -g > {shlex.quote(str(terminal_before))}; " + app_command + f"; status=$?; stty -g > {shlex.quote(str(terminal_after))}; exit $status"
    )

    def capture() -> str:
        return _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)

    def wait_for_screen(text: str, *, present: bool = True, timeout: float = 15) -> str:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = capture()
            if (text in pane) is present:
                return pane
            time.sleep(0.05)
        pytest.fail(
            f"native provider screen did not {'show' if present else 'hide'} {text!r}:\n"
            f"{pane}\nprovider_requests={json.dumps(provider.requests, indent=2, sort_keys=True)}\n"
            f"provider_response_content_types={json.dumps(provider.response_content_types)}\n"
            "provider_response_chunks="
            f"{json.dumps([body.decode() for body in provider.response_bodies], indent=2)}"
        )

    try:
        _tmux(socket, "new-session", "-d", "-s", "keepalive", "sleep", "60", env=env)
        _tmux(socket, "set-option", "-g", "exit-empty", "off", env=env)
        _tmux(socket, "set-option", "-g", "remain-on-exit", "on", env=env)
        _tmux(socket, "new-session", "-d", "-x", "80", "-y", "24", "-s", session, command, env=env)
        _tmux(socket, "kill-session", "-t", "keepalive", env=env)
        _tmux(socket, "set-option", "-g", "status", "off", env=env)
        wait_for_screen("VOIDCODE", timeout=30)

        _tmux(
            socket,
            "send-keys",
            "-t",
            f"{session}:0.0",
            "-l",
            "Please start a shell command that sleeps for fifteen seconds.",
            env=env,
        )
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        approval = wait_for_screen("y Allow", timeout=30)
        assert "shell_exec" in approval and "sleep 15" in approval
        assert "Waiting approval" in approval
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "y", env=env)
        wait_for_screen("y Allow", present=False, timeout=10)

        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 15
            summary = None
            replay = None
            while time.monotonic() < deadline:
                summaries = runtime.list_sessions()
                if summaries:
                    summary = summaries[0]
                    session_id = summary.session.id
                    replay = runtime.replay_session(session_id=session_id)
                    requests = [event for event in replay.events if event.event_type == "runtime.request_received"]
                    started = bool(requests) and any(
                        event.event_type == "runtime.tool_started" and event.sequence > requests[-1].sequence for event in replay.events
                    )
                    if str(summary.status) == "running" and started:
                        break
                time.sleep(0.05)
            else:
                event_details = [(event.sequence, event.event_type, event.payload) for event in replay.events] if replay is not None else []
                pytest.fail(
                    "provider shell call did not reach public tool_started state: "
                    f"status={getattr(summary, 'status', None)!r}; "
                    f"screen={capture()!r}; "
                    f"events={json.dumps(event_details, default=str)}; "
                    f"provider_requests={json.dumps(provider.requests, indent=2, sort_keys=True)}"
                )
            first_request = requests[-1]
            session_id = summary.session.id

        wait_for_screen("Running")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "continue", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        steering_screen = wait_for_screen("Steering queued (1)")
        assert "continue" in steering_screen and "Running" in steering_screen
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            queued_replay = runtime.replay_session(session_id=session_id)
            pending = queued_replay.session.metadata.get("pending_messages", [])
            assert isinstance(pending, list) and len(pending) == 1
            assert pending[0]["kind"] == "steering"
            assert pending[0]["content"] == "continue"
            steering_ids = tuple(message["id"] for message in pending)
            active_tool_started = next(
                event for event in queued_replay.events if event.event_type == "runtime.tool_started" and event.sequence > first_request.sequence
            )

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                cancelled_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                cancelled_replay = runtime.replay_session(session_id=session_id)
                cancelled = next(
                    (
                        event
                        for event in reversed(cancelled_replay.events)
                        if event.event_type == "runtime.failed" and event.payload.get("cancelled") is True
                    ),
                    None,
                )
                if str(cancelled_summary.status) == "interrupted" and cancelled is not None:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(
                    "native Escape did not interrupt the provider-backed run; "
                    f"status={cancelled_summary.status}; events="
                    f"{[(event.sequence, event.event_type) for event in cancelled_replay.events]!r}"
                )
            assert cancelled.payload["reason"] == "tui_turn_cancel"
            cancel_sequence = cancelled.sequence
            remaining = cancelled_replay.session.metadata.get("pending_messages", [])
            assert isinstance(remaining, list)
            assert tuple(message["id"] for message in remaining) == steering_ids
            tool_lifecycle = [event for event in cancelled_replay.events if event.event_type in {"runtime.tool_started", "runtime.tool_completed"}]
            assert [event.event_type for event in tool_lifecycle] == [
                "runtime.tool_started",
                "runtime.tool_completed",
            ]
            assert tool_lifecycle[0].sequence == active_tool_started.sequence
            assert tool_lifecycle[1].sequence < cancelled.sequence

        wait_for_screen("Running", present=False)
        wait_for_screen("Failed ·", present=False)
        wait_for_screen("Interrupted")
        reentry_prompt = "Please inspect restored.txt and report its contents."
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", reentry_prompt, env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        expected_prompt = f"{reentry_prompt}\n\nRuntime steering messages:\ncontinue"
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                reentry_replay = runtime.replay_session(session_id=session_id)
                reentry_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                reentry_request = next(
                    (
                        event
                        for event in reversed(reentry_replay.events)
                        if event.event_type == "runtime.request_received" and event.sequence > cancel_sequence
                    ),
                    None,
                )
                if reentry_request is not None:
                    reentry_tool_events = [
                        event
                        for event in reentry_replay.events
                        if event.sequence > reentry_request.sequence and event.event_type in {"runtime.tool_started", "runtime.tool_completed"}
                    ]
                    if len(reentry_tool_events) == 2 and str(reentry_summary.status) == "completed":
                        break
                time.sleep(0.05)
            else:
                pytest.fail(
                    "provider-backed TUI did not complete the queued-steering read; "
                    f"status={reentry_summary.status}; events="
                    f"{[(event.sequence, event.event_type) for event in reentry_replay.events]!r}"
                )
            assert reentry_request is not None
            assert reentry_request.payload["prompt"] == expected_prompt
            assert "pending_messages" not in reentry_replay.session.metadata
            assert [event.event_type for event in reentry_tool_events] == [
                "runtime.tool_started",
                "runtime.tool_completed",
            ]
            assert reentry_tool_events[-1].payload["tool"] == "read"
            read_output = reentry_tool_events[-1].payload.get("content")
            assert isinstance(read_output, str) and "POST_CANCEL_PROVIDER_READ" in read_output
            assert str(reentry_summary.status) == "completed"
        wait_for_screen("Read after cancellation.")

        (workspace / "restored.txt").write_text("POST_CANCEL_CLEAN_PROVIDER_READ\n")
        clean_prompt = "Please inspect restored.txt again and report its contents."
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", clean_prompt, env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                clean_replay = runtime.replay_session(session_id=session_id)
                clean_summary = next(item for item in runtime.list_sessions() if item.session.id == session_id)
                clean_request = next(
                    (
                        event
                        for event in reversed(clean_replay.events)
                        if event.event_type == "runtime.request_received" and event.sequence > reentry_request.sequence
                    ),
                    None,
                )
                if clean_request is not None:
                    clean_tool_events = [
                        event
                        for event in clean_replay.events
                        if event.sequence > clean_request.sequence and event.event_type in {"runtime.tool_started", "runtime.tool_completed"}
                    ]
                    if len(clean_tool_events) == 2 and str(clean_summary.status) == "completed":
                        break
                time.sleep(0.05)
            else:
                pytest.fail(
                    "provider-backed TUI did not complete the clean read; "
                    f"status={clean_summary.status}; events="
                    f"{[(event.sequence, event.event_type) for event in clean_replay.events]!r}"
                )
            assert clean_request is not None
            assert clean_request.payload["prompt"] == clean_prompt
            assert "pending_messages" not in clean_replay.session.metadata
            assert [event.event_type for event in clean_tool_events] == [
                "runtime.tool_started",
                "runtime.tool_completed",
            ]
            assert clean_tool_events[-1].payload["tool"] == "read"
            clean_output = clean_tool_events[-1].payload.get("content")
            assert isinstance(clean_output, str) and "POST_CANCEL_CLEAN_PROVIDER_READ" in clean_output
            assert str(clean_summary.status) == "completed"
        wait_for_screen("Read after clean re-entry.")
        assert len(provider.requests) == 5
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-c", "C-c", env=env)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            dead = _tmux(
                socket,
                "display-message",
                "-p",
                "-t",
                f"{session}:0.0",
                "#{pane_dead}",
                env=env,
            ).strip()
            if dead == "1":
                break
            time.sleep(0.05)
        assert dead == "1"
        assert terminal_before.read_text().strip() == terminal_after.read_text().strip()
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "-f", "/dev/null", "kill-server"],
            env=env,
            check=False,
            capture_output=True,
        )
        provider.shutdown()
        provider_thread.join(timeout=5)
        provider.server_close()


def test_native_tui_resume_preserves_completed_session_without_duplicate_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from voidcode.runtime.config.loading import load_runtime_config
    from voidcode.runtime.contracts import RuntimeRequest
    from voidcode.runtime.service import VoidCodeRuntime

    root = tmp_path
    workspace = root / "workspace"
    home = root / "home"
    config_home = root / "config"
    state_home = root / "state"
    cache_home = root / "cache"
    data_home = root / "data"
    local_app_data = root / "localappdata"
    tmux_tmpdir = root / "tmux"
    for directory in (workspace, home, config_home, state_home, cache_home, data_home, local_app_data, tmux_tmpdir):
        directory.mkdir()
    (workspace / "resume.txt").write_text("resumed file bytes\n")
    provider = OpenAILoopbackServer(
        [
            openai_response(
                message={
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-read-resume",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "resume.txt"}),
                            },
                        }
                    ],
                },
                finish="tool_calls",
            ),
            openai_response(message={"role": "assistant", "content": "done"}, finish="stop"),
        ]
    )
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "loopback-key")
    try:
        provider_thread.start()
        configure_openai_provider(workspace, f"http://127.0.0.1:{provider.server_address[1]}/v1")
        workspace_config = json.loads((workspace / ".voidcode.json").read_text(encoding="utf-8"))
        workspace_config["approval_mode"] = "yolo"
        workspace_config["tui"] = {"keymap": {"ctrl+r": "app.session.resume"}}
        (workspace / ".voidcode.json").write_text(json.dumps(workspace_config), encoding="utf-8")
        project_root = Path(__file__).resolve().parents[3]
        socket = root / "tmux.sock"
        session = "native-resume"
        database = root / "voidcode.sqlite3"
        env = {
            "DEEPSEEK_API_KEY": "loopback-key",
            "APPDATA": str(config_home),
            "HOME": str(home),
            "LOCALAPPDATA": str(local_app_data),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(project_root / "src"),
            "TERM": "tmux-256color",
            "TMUX_TMPDIR": str(tmux_tmpdir),
            "USERPROFILE": str(home),
            "VOIDCODE_APPROVAL_MODE": "yolo",
            "VOIDCODE_DB_PATH": str(database),
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_STATE_HOME": str(state_home),
        }
        monkeypatch.setenv("VOIDCODE_DB_PATH", str(database))
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            seeded = runtime.run(RuntimeRequest(prompt="read resume.txt", allocate_session_id=True))
            assert seeded.session.status == "completed"
            session_id = seeded.session.session.id
            seeded_replay = runtime.replay_session(session_id=session_id)
            completed_read = next(event for event in seeded_replay.events if event.event_type == "runtime.tool_completed")
            assert "resumed file bytes" in str(completed_read.payload.get("content", ""))
            assert "done" in (seeded_replay.output or "")
            assert sum(event.event_type == "runtime.tool_started" for event in seeded_replay.events) == 1
            assert sum(event.event_type == "runtime.tool_completed" for event in seeded_replay.events) == 1
            assert len(provider.requests) == 2
    finally:
        if provider_thread.is_alive():
            provider.shutdown()
            provider_thread.join(timeout=5)
        provider.server_close()

    command = (
        "env -i "
        + " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        + " "
        + shlex.quote(sys.executable)
        + " -m voidcode tui --workspace "
        + shlex.quote(str(workspace))
    )

    def capture(*args: str) -> str:
        return _tmux(socket, "capture-pane", "-p", *args, "-t", f"{session}:0.0", env=env)

    def wait_for_screen(text: str, *, present: bool = True, timeout: float = 20) -> str:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = capture()
            if (text in pane) is present:
                return pane
            time.sleep(0.1)
        pytest.fail(f"native resume screen did not {'show' if present else 'hide'} {text!r}:\n{pane}\nscrollback:\n{capture('-S', '-')}")

    try:
        _tmux(socket, "new-session", "-d", "-s", "keepalive", "sleep", "60", env=env)
        _tmux(socket, "set-option", "-g", "exit-empty", "off", env=env)
        _tmux(socket, "set-option", "-g", "remain-on-exit", "on", env=env)
        _tmux(socket, "new-session", "-d", "-x", "80", "-y", "24", "-s", session, command, env=env)
        _tmux(socket, "kill-session", "-t", "keepalive", env=env)
        _tmux(socket, "set-option", "-g", "status", "off", env=env)
        wait_for_screen("VOIDCODE")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-r", env=env)
        picker = wait_for_screen("Select Session")
        assert "read resume.txt" in picker
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        wait_for_screen("Read · resume.txt", timeout=30)
        pane = wait_for_screen("done")
        assert "Traceback" not in pane
        scrollback = capture("-S", "-")
        assert "Read · resume.txt" in scrollback and "done" in scrollback
        assert "to expand" not in scrollback.lower()
        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            restored_replay = runtime.replay_session(session_id=session_id)
            assert restored_replay.events == seeded_replay.events
            assert restored_replay.output == seeded_replay.output
            assert sum(event.event_type == "runtime.tool_started" for event in restored_replay.events) == 1
            assert sum(event.event_type == "runtime.tool_completed" for event in restored_replay.events) == 1
            assert len(provider.requests) == 2
            restored = runtime.list_sessions()
            assert any(item.session.id == session_id and str(item.status) == "completed" for item in restored)
        print(
            json.dumps(
                {
                    "prompt": "read resume.txt",
                    "replay_unchanged": True,
                    "resumed_session": session_id,
                },
                sort_keys=True,
            )
        )
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "-f", "/dev/null", "kill-server"],
            env=env,
            check=False,
            capture_output=True,
        )


def test_native_ctrl_t_tree_filter_moves_native_caret_and_cancels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    workspace = root / "workspace"
    directories = ("home", "config", "state", "cache", "data", "localappdata", "tmux")
    for directory in (workspace, *(root / name for name in directories)):
        directory.mkdir(parents=True)
    prompt_bytes = "run printf '👩‍💻中文e\u0301\t'\r\nrun printf tree_Native_READY\r  "
    prompt_text = prompt_bytes.replace("\r\n", "\n").replace("\r", "\n")
    prompt_buffer = root / "prompt.txt"
    prompt_buffer.write_bytes(prompt_bytes.encode("utf-8"))
    provider = OpenAILoopbackServer(
        [
            openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-native-tree",
                            "type": "function",
                            "function": {
                                "name": "shell_exec",
                                "arguments": json.dumps({"command": "printf tree_Native_READY"}),
                            },
                        }
                    ],
                },
                finish="tool_calls",
            ),
            openai_response(message={"role": "assistant", "content": "tree_Native_READY"}, finish="stop"),
        ]
    )
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    configure_openai_provider(workspace, f"http://127.0.0.1:{provider.server_address[1]}/v1")
    config = json.loads((workspace / ".voidcode.json").read_text(encoding="utf-8"))
    config["approval_mode"] = "yolo"
    config["tui"] = {"keymap": {"ctrl+t": "app.session.tree"}}
    (workspace / ".voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    project_root = Path(__file__).resolve().parents[3]
    env = {
        "APPDATA": str(root / "config"),
        "HOME": str(root / "home"),
        "LOCALAPPDATA": str(root / "localappdata"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root / "src"),
        "TERM": "tmux-256color",
        "USERPROFILE": str(root / "home"),
        "DEEPSEEK_API_KEY": "loopback-key",
        "VOIDCODE_APPROVAL_MODE": "yolo",
        "VOIDCODE_DB_PATH": str(root / "voidcode.sqlite3"),
        "TMUX_TMPDIR": str(root / "tmux"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
    }
    monkeypatch.setenv("XDG_STATE_HOME", env["XDG_STATE_HOME"])
    monkeypatch.setenv("VOIDCODE_DB_PATH", env["VOIDCODE_DB_PATH"])
    socket = root / "tmux.sock"
    session = "native-tree"
    command = (
        "env -i "
        + " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        + " "
        + shlex.quote(sys.executable)
        + " -m voidcode tui --workspace "
        + shlex.quote(str(workspace))
    )

    def capture(*args: str) -> str:
        return _tmux(socket, "capture-pane", "-p", *args, "-t", f"{session}:0.0", env=env)

    def wait_for_screen(text: str, *, present: bool = True, timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = capture()
            if (text in pane) is present:
                return pane
            time.sleep(0.1)
        pytest.fail(f"native Ctrl+T screen did not {'show' if present else 'hide'} {text!r}:\n{pane}\n{capture('-S', '-')}")

    try:
        provider_thread.start()
        _tmux(socket, "new-session", "-d", "-s", "keepalive", "sleep", "60", env=env)
        _tmux(socket, "set-option", "-g", "exit-empty", "off", env=env)
        _tmux(socket, "set-option", "-g", "remain-on-exit", "on", env=env)
        _tmux(socket, "new-session", "-d", "-x", "80", "-y", "24", "-s", session, command, env=env)
        _tmux(socket, "kill-session", "-t", "keepalive", env=env)
        _tmux(socket, "set-option", "-g", "status", "off", env=env)
        wait_for_screen("VOIDCODE", timeout=30)
        _tmux(socket, "load-buffer", "-b", "native-prompt", str(prompt_buffer), env=env)
        _tmux(socket, "paste-buffer", "-p", "-r", "-b", "native-prompt", "-t", f"{session}:0.0", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        wait_for_screen("tree_Native_READY", timeout=30)

        from voidcode.runtime.config.loading import load_runtime_config
        from voidcode.runtime.service import VoidCodeRuntime
        from voidcode.runtime.storage import SqliteSessionStore

        with VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace)) as runtime:
            store = SqliteSessionStore()
            summary = None
            checkpoint = None
            replay = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                summaries = runtime.list_sessions()
                if summaries:
                    summary = summaries[0]
                    checkpoint = store.load_resume_checkpoint(workspace=workspace, session_id=summary.session.id)
                    if checkpoint is not None and checkpoint["kind"] == "terminal":
                        replay = runtime.replay_session(session_id=summary.session.id)
                        break
                time.sleep(0.1)
            else:
                checkpoint_kind = None if checkpoint is None else checkpoint.get("kind")
                pytest.fail(
                    "public runtime did not complete native tree test request "
                    f"(status={None if summary is None else summary.status!s}, checkpoint={checkpoint_kind!r})"
                )
            assert summary is not None
            assert checkpoint is not None and checkpoint["kind"] == "terminal"
            assert replay is not None
            assert any(event.event_type == "core.response_ready" for event in replay.events)
            request = next(event for event in replay.events if event.event_type == "runtime.request_received")
            assert request.payload["prompt"] == prompt_text

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-t", env=env)
        tree = wait_for_screen("Session Entries")
        assert "run printf" in tree
        assert "tree_Native_READY" in tree and "👩‍💻中文e\u0301" in tree
        before = tuple(
            map(
                int,
                _tmux(
                    socket,
                    "display-message",
                    "-p",
                    "-t",
                    f"{session}:0.0",
                    "#{cursor_x},#{cursor_y}",
                    env=env,
                )
                .strip()
                .split(","),
            )
        )
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "printf", env=env)
        filtered = wait_for_screen("> printf")
        filter_lines = filtered.splitlines()
        filter_row = next(index for index, line in enumerate(filter_lines) if "> printf" in line)
        filter_line = filter_lines[filter_row]
        filter_anchor = filter_line.index("> printf")
        filter_cursor = (visible_width(filter_line[:filter_anchor]) + visible_width("> printf"), filter_row)
        deadline = time.monotonic() + 10
        after = before
        while time.monotonic() < deadline:
            after = tuple(
                map(
                    int,
                    _tmux(
                        socket,
                        "display-message",
                        "-p",
                        "-t",
                        f"{session}:0.0",
                        "#{cursor_x},#{cursor_y}",
                        env=env,
                    )
                    .strip()
                    .split(","),
                )
            )
            if after == filter_cursor:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"cursor missed filter cell: {before!r}->{after!r}; expected={filter_cursor!r}; pane:\n{filtered}")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        restored = wait_for_screen("Session Entries", present=False)
        dead = _tmux(socket, "display-message", "-p", "-t", f"{session}:0.0", "#{pane_dead}", env=env).strip()
        assert dead == "0"
        assert "Traceback" not in restored
        print(
            json.dumps(
                {
                    "action": "Ctrl+T app.session.tree",
                    "filter_caret": [before, after],
                    "filtered_tree": filtered,
                    "runtime_prompt": request.payload["prompt"],
                    "checkpoint_kind": checkpoint["kind"],
                    "tree_cancelled": True,
                },
                sort_keys=True,
            )
        )
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "-f", "/dev/null", "kill-server"],
            env=env,
            check=False,
            capture_output=True,
        )
        if provider_thread.is_alive():
            provider.shutdown()
            provider_thread.join(timeout=5)
        provider.server_close()


def test_native_question_escape_cancels_through_the_public_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path
    workspace = root / "workspace"
    home = root / "home"
    config_home = root / "config"
    state_home = root / "state"
    cache_home = root / "cache"
    data_home = root / "data"
    local_app_data = root / "localappdata"
    tmux_tmpdir = root / "tmux"
    for directory in (workspace, home, config_home, state_home, cache_home, data_home, local_app_data, tmux_tmpdir):
        directory.mkdir()
    project_root = Path(__file__).resolve().parents[3]
    socket = root / "t"
    session = "native-question"
    database = root / "voidcode.sqlite3"
    env = {
        "APPDATA": str(config_home),
        "HOME": str(home),
        "LOCALAPPDATA": str(local_app_data),
        "NATIVE_WORKSPACE": str(workspace),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root / "src"),
        "TERM": "tmux-256color",
        "TMUX_TMPDIR": str(tmux_tmpdir),
        "USERPROFILE": str(home),
        "VOIDCODE_APPROVAL_MODE": "yolo",
        "VOIDCODE_DB_PATH": str(database),
        "XDG_CACHE_HOME": str(cache_home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_STATE_HOME": str(state_home),
    }
    monkeypatch.setenv("VOIDCODE_DB_PATH", str(database))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    command = (
        "env -i "
        + " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        + " "
        + shlex.quote(sys.executable)
        + " -c "
        + shlex.quote(_QUESTION_CHILD)
    )

    def wait_for_screen(text: str, timeout: float = 30) -> str:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
            if text in pane:
                return pane
            time.sleep(0.1)
        scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
        pytest.fail(f"native question screen omitted {text!r}:\n{pane}\nscrollback:\n{scrollback}")

    from voidcode.runtime.config.loading import RuntimeConfig
    from voidcode.runtime.service import VoidCodeRuntime

    def resize(width: int, height: int) -> None:
        _tmux(socket, "resize-window", "-t", session, "-x", str(width), "-y", str(height), env=env)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            size = _tmux(
                socket,
                "display-message",
                "-p",
                "-t",
                f"{session}:0.0",
                "#{pane_width}x#{pane_height}",
                env=env,
            ).strip()
            if size == f"{width}x{height}":
                return
            time.sleep(0.05)
        pytest.fail(f"native question pane did not reach {width}x{height}")

    try:
        _tmux(socket, "new-session", "-d", "-s", "keepalive", "sleep", "60", env=env)
        _tmux(socket, "set-option", "-g", "exit-empty", "off", env=env)
        _tmux(socket, "set-option", "-g", "remain-on-exit", "on", env=env)
        _tmux(socket, "new-session", "-d", "-x", "80", "-y", "24", "-s", session, command, env=env)
        _tmux(socket, "kill-session", "-t", "keepalive", env=env)
        wait_for_screen("VOIDCODE")
        resize(20, 6)

        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "ask", env=env)
        wait_for_screen("ask")
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        answer_screen = wait_for_screen("Choose a path", timeout=30)
        assert "e\u0301" in answer_screen
        question_actions_visible = "Enter select" in answer_screen and "Esc cancel" in answer_screen
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        question_20 = wait_for_screen("Cancel at 20x6", timeout=30)
        assert "Yes" in question_20
        question_actions_visible &= "Enter select" in question_20 and "Esc cancel" in question_20
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)
        question_10 = wait_for_screen("Cancel below min", timeout=30)
        resize(10, 5)
        hold_screen = wait_for_screen("Resize to")

        def public_question_state() -> tuple[object, ...]:
            with VoidCodeRuntime(
                workspace=workspace,
                config=RuntimeConfig(approval_mode="yolo"),
            ) as runtime:
                summary = runtime.list_sessions()[0]
                replay = runtime.replay_session(session_id=summary.session.id)
                return (
                    tuple((event.sequence, event.event_type, event.payload) for event in replay.events),
                    str(summary.status),
                    summary.prompt,
                )

        pending_state = public_question_state()
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "ZZZ", env=env)
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)
        assert wait_for_screen("Resize to") == hold_screen
        assert public_question_state() == pending_state
        _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Escape", env=env)

        with VoidCodeRuntime(
            workspace=workspace,
            config=RuntimeConfig(approval_mode="yolo"),
        ) as runtime:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                summary = runtime.list_sessions()[0]
                replay = runtime.replay_session(session_id=summary.session.id)
                requested = [event for event in replay.events if event.event_type == "runtime.question_requested"]
                answered = [event for event in replay.events if event.event_type == "runtime.question_answered"]
                if str(summary.status) == "completed" and len(answered) == 3:
                    break
                time.sleep(0.05)
            else:
                pane = _tmux(socket, "capture-pane", "-p", "-t", f"{session}:0.0", env=env)
                scrollback = _tmux(socket, "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0", env=env)
                pytest.fail(f"native question answer/cancel flow did not complete:\n{pane}\nscrollback:\n{scrollback}")
            assert len(requested) == 3
            assert answered[0].payload["responses"] == [{"header": "Path", "answers": ["e\u0301"]}]
            assert answered[1].payload == {
                "request_id": requested[1].payload["request_id"],
                "cancelled": True,
            }
            assert answered[2].payload == {
                "request_id": requested[2].payload["request_id"],
                "cancelled": True,
            }
            request = next(event for event in replay.events if event.event_type == "runtime.request_received")
            assert request.payload["prompt"] == "ask"
            assert replay.session.status == "completed"
            assert question_actions_visible
        print(
            json.dumps(
                {
                    "native_terminal": "tmux",
                    "question_screens": [answer_screen, question_20, question_10],
                    "question_results": [event.payload for event in answered],
                    "request_prompt": request.payload["prompt"],
                    "session_status": str(replay.session.status),
                },
                sort_keys=True,
            )
        )
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "-f", "/dev/null", "kill-server"],
            env=env,
            check=False,
            capture_output=True,
        )


def test_native_tui_renders_all_glyph_presets_on_real_tool_output(tmp_path: Path) -> None:
    presets = get_args(TuiSymbolsPreset.__value__)
    assert len(presets) == 3
    observed: dict[str, str] = {}

    for preset in presets:
        root = tmp_path / preset
        workspace = root / "workspace"
        home = root / "home"
        config_home = root / "config"
        state_home = root / "state"
        cache_home = root / "cache"
        data_home = root / "data"
        tmux_tmpdir = root / "tmux"
        local_app_data = root / "localappdata"
        for directory in (
            workspace,
            home,
            config_home,
            state_home,
            cache_home,
            data_home,
            local_app_data,
            tmux_tmpdir,
        ):
            directory.mkdir(parents=True)
        (workspace / "glyph.txt").write_text("glyph file line one\nglyph file line two\n")
        config_path = workspace / ".voidcode.json"
        provider = OpenAILoopbackServer(
            [
                openai_response(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call-glyph-{preset}",
                                "type": "function",
                                "function": {
                                    "name": "shell_exec",
                                    "arguments": json.dumps({"command": "sleep 1; cat glyph.txt"}),
                                },
                            }
                        ],
                    },
                    finish="tool_calls",
                ),
                openai_response(message={"role": "assistant", "content": "Read glyph.txt."}, finish="stop"),
            ]
        )
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        configure_openai_provider(workspace, f"http://127.0.0.1:{provider.server_address[1]}/v1")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["approval_mode"] = "yolo"
        config["tui"] = {"theme": {"symbols": {"preset": preset}}}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        project_root = Path(__file__).resolve().parents[3]
        env = {
            "APPDATA": str(config_home),
            "HOME": str(home),
            "LOCALAPPDATA": str(local_app_data),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(project_root / "src"),
            "TERM": "tmux-256color",
            "TMUX_TMPDIR": str(tmux_tmpdir),
            "USERPROFILE": str(home),
            "DEEPSEEK_API_KEY": "loopback-key",
            "VOIDCODE_APPROVAL_MODE": "yolo",
            "VOIDCODE_DB_PATH": str(root / "voidcode.sqlite3"),
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_STATE_HOME": str(state_home),
        }
        socket = root / "tmux.sock"
        session = "native-glyph"
        command = (
            "env -i "
            + " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
            + " "
            + shlex.quote(sys.executable)
            + " -m voidcode tui --workspace "
            + shlex.quote(str(workspace))
        )
        try:
            provider_thread.start()
            _tmux(socket, "new-session", "-d", "-s", "keepalive", "sleep", "60", env=env)
            _tmux(socket, "set-option", "-g", "exit-empty", "off", env=env)
            _tmux(socket, "set-option", "-g", "remain-on-exit", "on", env=env)
            _tmux(socket, "new-session", "-d", "-x", "80", "-y", "24", "-s", session, command, env=env)
            _tmux(socket, "kill-session", "-t", "keepalive", env=env)
            _tmux(socket, "set-option", "-g", "status", "off", env=env)

            def capture(
                *args: str,
                _socket: Path = socket,
                _session: str = session,
                _env: dict[str, str] = env,
            ) -> str:
                return _tmux(_socket, "capture-pane", "-p", *args, "-t", f"{_session}:0.0", env=_env)

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                pane = capture()
                if "VOIDCODE" in pane:
                    break
                time.sleep(0.1)
            else:
                pytest.fail(f"native glyph TUI did not start for {preset}:\n{capture('-S', '-')}")
            brand_mark = "[*V]" if preset == "ascii" else "◇V"
            assert any(line.strip() == f"{brand_mark}  VOIDCODE" for line in pane.splitlines())

            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "-l", "show glyph.txt", env=env)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pane = capture()
                if "show glyph.txt" in pane:
                    break
                time.sleep(0.05)
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "Enter", env=env)

            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                pane = capture()
                if "Running" in pane:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"native shell tool did not start for glyph preset {preset}:\n{capture()}")
            _tmux(socket, "send-keys", "-t", f"{session}:0.0", "C-o", env=env)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pane = capture()
                if "Tool output expansion enabled" in pane:
                    break
                time.sleep(0.05)
            else:
                pytest.fail(f"native expansion toggle was not acknowledged before tool output:\n{capture()}")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                scrollback = capture("-S", "-")
                if "glyph file line two" in scrollback and "Read glyph.txt." in scrollback:
                    break
                time.sleep(0.1)
            else:
                pytest.fail(f"native tool body did not expand for glyph preset {preset}:\n{capture()}\nscrollback:\n{scrollback}")
            assert "glyph file line one" in scrollback and "Read glyph.txt." in scrollback
            assert not any(line.strip() == f"{brand_mark}  VOIDCODE" for line in capture().splitlines())
            glyph = resolve_theme(glyph_preset=preset).symbol("status.success")
            assert glyph in scrollback
            observed[preset] = glyph
        finally:
            if provider_thread.is_alive():
                provider.shutdown()
                provider_thread.join(timeout=5)
            provider.server_close()
            subprocess.run(
                ["tmux", "-S", str(socket), "-f", "/dev/null", "kill-server"],
                env=env,
                check=False,
                capture_output=True,
            )

    assert len(set(observed.values())) == len(presets)
    print(json.dumps({"glyph_presets": observed}, ensure_ascii=True, sort_keys=True))
