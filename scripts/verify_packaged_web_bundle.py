from __future__ import annotations

import io
import os
import selectors
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
import zipfile
from pathlib import Path


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _assert_wheel_contains_web_bundle(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as archive:
        members = set(archive.namelist())
    if "voidcode/_web_dist/index.html" not in members:
        raise SystemExit(f"wheel is missing packaged frontend index.html: {wheel_path}")
    if not any(member.startswith("voidcode/_web_dist/assets/") for member in members):
        raise SystemExit(f"wheel is missing packaged frontend assets: {wheel_path}")


def _wait_for_url(process: subprocess.Popen[str]) -> str:
    stdout = process.stdout
    if stdout is None:
        raise SystemExit("packaged launcher stdout pipe is unavailable")
    return _wait_for_url_from_stream(stdout=stdout, poll=process.poll, timeout_seconds=30.0)


def _wait_for_url_from_stream(
    *,
    stdout: io.TextIOBase,
    poll: object,
    timeout_seconds: float,
) -> str:
    buffered_lines: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    poll_fn = poll
    if not callable(poll_fn):
        raise TypeError("poll must be callable")
    with selectors.DefaultSelector() as selector:
        selector.register(stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.0)
            ready = selector.select(timeout=min(remaining, 0.1))
            if not ready:
                if poll_fn() is not None:
                    output = "".join(buffered_lines).strip()
                    raise SystemExit(
                        "packaged launcher exited before printing its local URL"
                        + (f"\nlauncher output:\n{output}" if output else "")
                    )
                continue
            line = stdout.readline()
            if not line:
                if poll_fn() is not None:
                    output = "".join(buffered_lines).strip()
                    raise SystemExit(
                        "packaged launcher exited before printing its local URL"
                        + (f"\nlauncher output:\n{output}" if output else "")
                    )
                continue
            buffered_lines.append(line)
            if "Local server running at:" in line:
                return line.split("Local server running at:", 1)[1].strip()
    raise SystemExit("timed out waiting for packaged launcher URL")


def _probe_launcher(url: str) -> None:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read(200).decode("utf-8", errors="replace")
                if response.status != 200:
                    raise SystemExit(f"packaged launcher returned HTTP {response.status}")
                if "<!DOCTYPE html>" not in body:
                    raise SystemExit("packaged launcher did not return frontend HTML")
                return
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f"timed out probing packaged launcher URL: {url}")


def _verify_installed_launcher(wheel_path: Path) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        venv_dir = root / "venv"
        workspace = root / "workspace"
        workspace.mkdir()
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        python = _venv_python(venv_dir)
        subprocess.run(
            [str(python), "-m", "pip", "install", str(wheel_path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "voidcode",
                "web",
                "--workspace",
                str(workspace),
                "--host",
                "127.0.0.1",
                "--no-open",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            url = _wait_for_url(process)
            _probe_launcher(url)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_packaged_web_bundle.py <wheel-path>")
    wheel_path = Path(sys.argv[1]).resolve()
    _assert_wheel_contains_web_bundle(wheel_path)
    _verify_installed_launcher(wheel_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
