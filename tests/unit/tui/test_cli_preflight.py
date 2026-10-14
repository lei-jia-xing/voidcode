import errno
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("scope", ["user", "workspace"])
@pytest.mark.parametrize("is_tty", [False, True])
def test_invalid_tui_config_exits_before_native_terminal_setup(tmp_path: Path, scope: str, is_tty: bool) -> None:
    root = tmp_path
    home = root / "home"
    config_home = root / "config"
    workspace = root / "workspace"
    local_app_data = root / "localappdata"
    for directory in (home, config_home, workspace, local_app_data):
        directory.mkdir()
    config_path = config_home / "voidcode" / "config.json" if scope == "user" else workspace / ".voidcode.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"tui": {"theme": {"mode": "neon"}}}))

    env = {
        "APPDATA": str(config_home),
        "HOME": str(home),
        "LOCALAPPDATA": str(local_app_data),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        "USERPROFILE": str(home),
        "VOIDCODE_DB_PATH": str(root / "voidcode.sqlite3"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_STATE_HOME": str(root / "state"),
    }
    if os.name == "nt":
        env.update({name: os.environ[name] for name in ("SystemRoot", "WINDIR", "COMSPEC") if name in os.environ})
    command = [sys.executable, "-m", "voidcode", "tui", "--workspace", str(workspace)]
    master = slave = process = None
    try:
        if is_tty:
            if os.name != "posix":
                pytest.skip("PTY CLI path requires POSIX")
            import fcntl
            import pty
            import struct
            import termios

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            original_termios = termios.tcgetattr(slave)
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[3],
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
            os.set_blocking(master, False)
            output = bytearray()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break
                elif process.poll() is not None:
                    break
            returncode = process.wait(timeout=5)
            assert termios.tcgetattr(slave) == original_termios
        else:
            result = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[3],
                env=env,
                capture_output=True,
                timeout=30,
                check=False,
            )
            returncode = result.returncode
            output = bytearray(result.stdout + result.stderr)

        assert returncode == 2
        assert str(config_path).encode() in output
        assert b"tui.theme.mode" in output and b"neon" in output
        assert b"Traceback" not in output
        assert b"\x1b" not in output
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if slave is not None:
            os.close(slave)
        if master is not None:
            os.close(master)
