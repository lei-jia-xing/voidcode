from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("kitty") is None or not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")),
    reason="requires a POSIX host with Kitty and a graphical display",
)


_CHILD = textwrap.dedent(
    r"""
import json
import os
import re
import termios
import time
from pathlib import Path

from voidcode.tui.region import TuiFrame
from voidcode.tui.term import Terminal

result_path = Path(os.environ["NATIVE_RESULT"])
done_path = Path(os.environ["NATIVE_DONE"])
stop_path = Path(os.environ["NATIVE_STOP"])
baseline = termios.tcgetattr(0)
terminal = None
result = {"term": os.environ.get("TERM"), "window_id": os.environ.get("KITTY_WINDOW_ID")}

try:
    terminal = Terminal.open()
    result["size"] = [terminal.width, terminal.height]

    def cursor_position():
        terminal._emit("\x1b[6n")
        received = bytearray()
        deadline = time.monotonic() + 2
        response = re.compile(rb"\x1b\[(\d+);(\d+)R")
        while time.monotonic() < deadline:
            chunk = terminal._read_input_chunk(max(0.0, deadline - time.monotonic()))
            if chunk is None:
                continue
            if not chunk:
                raise RuntimeError("terminal input ended before DSR response")
            received.extend(chunk)
            match = response.search(received)
            if match is not None:
                return [int(match.group(1)), int(match.group(2))]
        raise RuntimeError(f"Kitty did not answer DSR: {bytes(received)!r}")

    rows = ("VC_NATIVE_ROW_0", "VC_NATIVE_ROW_1", "VC_NATIVE_ROW_2")
    result["origin"] = cursor_position()
    terminal.paint_frame(TuiFrame(chrome_rows=rows, cursor=(2, 3)))
    result["first"] = cursor_position()
    terminal.paint_frame(TuiFrame(chrome_rows=rows, cursor=(1, 1)))
    result["second"] = cursor_position()
    result["rows"] = rows
except BaseException as error:
    result["error"] = f"{type(error).__name__}: {error}"
finally:
    result_path.write_text(json.dumps(result), encoding="utf-8")
    while not stop_path.exists():
        time.sleep(0.02)
    if terminal is not None:
        try:
            terminal.close()
        except BaseException as error:
            result["close_error"] = f"{type(error).__name__}: {error}"
    result["termios_restored"] = termios.tcgetattr(0) == baseline
    done_path.write_text(json.dumps(result), encoding="utf-8")
    """
)


def test_real_kitty_reanchors_cursor_on_cursor_only_repaint() -> None:
    with tempfile.TemporaryDirectory(prefix="vc-k-", dir="/tmp") as directory:
        root = Path(directory)
        home = root / "home"
        for name in ("home", "config", "cache", "data", "state", "appdata", "localappdata"):
            (root / name).mkdir()
        socket = root / "k"
        result_path = root / "result.json"
        done_path = root / "done.json"
        stop_path = root / "stop"
        stdout_path = root / "stdout"
        stderr_path = root / "stderr"
        env = {
            key: os.environ[key]
            for key in (
                "PATH",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS",
                "DISPLAY",
                "XAUTHORITY",
            )
            if key in os.environ
        }
        env.update(
            {
                "APPDATA": str(root / "appdata"),
                "HOME": str(home),
                "LOCALAPPDATA": str(root / "localappdata"),
                "NATIVE_DONE": str(done_path),
                "NATIVE_RESULT": str(result_path),
                "NATIVE_STOP": str(stop_path),
                "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
                "VOIDCODE_DB_PATH": str(root / "voidcode.sqlite3"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
            }
        )
        kitty = shutil.which("kitty")
        assert kitty is not None
        kitty_version = subprocess.run([kitty, "--version"], check=True, capture_output=True, text=True).stdout.strip()
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [
                    kitty,
                    "--single-instance=no",
                    "--config",
                    "NONE",
                    "--start-as=hidden",
                    "--listen-on",
                    f"unix:{socket}",
                    "-o",
                    "allow_remote_control=yes",
                    "-o",
                    "initial_window_width=80c",
                    "-o",
                    "initial_window_height=24c",
                    sys.executable,
                    "-c",
                    _CHILD,
                ],
                env=env,
                stdout=stdout,
                stderr=stderr,
            )
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not result_path.exists():
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                assert result_path.exists(), (
                    "Kitty child did not report the active DSR probe; "
                    f"stdout={stdout_path.read_text(errors='replace')!r}, "
                    f"stderr={stderr_path.read_text(errors='replace')!r}"
                )
                result = json.loads(result_path.read_text(encoding="utf-8"))
                captured = subprocess.run(
                    [
                        kitty,
                        "@",
                        "--to",
                        f"unix:{socket}",
                        "get-text",
                        "--match",
                        f"id:{result['window_id']}",
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
                ansi = subprocess.run(
                    [
                        kitty,
                        "@",
                        "--to",
                        f"unix:{socket}",
                        "get-text",
                        "--ansi",
                        "--add-cursor",
                        "--match",
                        f"id:{result['window_id']}",
                    ],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout
                stop_path.touch()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not done_path.exists():
                    time.sleep(0.05)
                assert done_path.exists(), "Kitty child did not report close/termios restoration"
                result = json.loads(done_path.read_text(encoding="utf-8"))
                evidence = {
                    **result,
                    "kitty_version": kitty_version,
                    "display_server": "wayland" if "WAYLAND_DISPLAY" in env else "x11",
                    "screen_while_terminal_open": captured,
                    "ansi_screen_with_cursor_while_terminal_open": ansi,
                }
                print(json.dumps(evidence, sort_keys=True))
                assert "error" not in result, result
                assert "close_error" not in result, result
                assert all(row in captured for row in result["rows"]), captured
                origin_row, _origin_column = result["origin"]
                assert result["size"] == [80, 24], result
                assert result["first"] == [origin_row + 2, 4], result
                assert result["second"] == [origin_row + 1, 2], result
                assert result["termios_restored"] is True, result

            finally:
                stop_path.touch()
                if process.poll() is None:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
