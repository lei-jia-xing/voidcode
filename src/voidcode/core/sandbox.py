"""Transparent exec backend: neutral confinement for registry children.

No plugins involved: when a ``SandboxPolicy`` is present, the registry builds the
helper argv + explicit env + protected paths from neutral inputs (workspace, plugin
source roots, helper path, extra protected paths from bootstrap) and runs every
child under it. Absent policy means today's host behavior exactly.

V1 scope is registry children ONLY (shell_exec, background, user_shell, LSP
services). Hooks executor, review git, and MCP stdio stay host-side with zero
plugin changes. File tools stay host-side and self-report; the engine dedups
overlap with supervisor observations.
"""

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from voidcode.sdk.observed_effects import ObservedEffects

if TYPE_CHECKING:
    from .activation import SandboxPolicy

#: Variables the child inherits from the deliberate baseline only — never the parent env.
_ENV_BASELINE_KEYS: tuple[str, ...] = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "TZ", "LANG", "LC_ALL")
#: Loader/injection variables dropped even from explicit overrides.
_DROPPED_ENV_KEYS: frozenset[str] = frozenset(
    {"LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_CONFIG_FILE", "PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH"}
)


@dataclass(frozen=True, slots=True)
class SandboxBackend:
    """Neutral confinement inputs for one runtime generation. None means unconfined."""

    policy: SandboxPolicy
    helper_path: str
    protected_paths: tuple[str, ...]
    env_baseline: tuple[tuple[str, str], ...] = ()


def check_helper(helper: Path) -> dict[str, object]:
    """Availability probe: the helper runs and reports a usable native backend.

    Bootstrap runs this when a sandbox section is configured; a missing backend
    refuses open with a clear error naming the missing piece. No silent degrade.
    """
    try:
        completed = subprocess.run(
            [str(helper), "probe"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"sandbox helper probe failed to run ({helper}): {exc}") from exc
    try:
        report = json.loads(completed.stdout.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"sandbox helper probe returned invalid JSON ({helper}): {exc}") from exc
    if not isinstance(report, dict) or not report.get("usable"):
        raise ValueError(
            f"sandbox confinement is unavailable on this host ({helper}): {report}. Remove the sandbox section to run without confinement."
        )
    return {str(key): value for key, value in report.items()}


def native_backend() -> str:
    if os.name == "nt":
        return "processcontainer"
    import sys

    if sys.platform == "darwin":
        return "seatbelt"
    return "bubblewrap"


def build_plan(
    backend: SandboxBackend,
    *,
    argv: tuple[str, ...],
    cwd: str,
    env_overrides: dict[str, str],
    workspace: str,
    scratch: str,
) -> tuple[tuple[str, ...], str, dict[str, str], bytes]:
    """Build helper argv + cwd + explicit full env + framed stdin payload for one exec.

    Pure data: enforcement happens inside the helper child after spawn.
    """
    policy = backend.policy
    env = _child_env(dict(backend.env_baseline), env_overrides, scratch=scratch, cwd=cwd)
    # The helper binary stays EXECUTABLE inside the sandbox (it re-runs as the
    # supervisor): protected by the read-only bind of its own directory, not by the
    # denied-path mask (which would make it un-executable).
    denied = [path for path in backend.protected_paths if path != backend.helper_path]
    payload = {
        "policy": {
            "mode": policy.mode,
            "network": policy.network,
            "workspace": workspace,
            "writable_roots": list(policy.writable_roots),
            "protected_paths": denied,
            "scratch": scratch,
        },
        "argv": list(argv),
        "cwd": cwd,
        "env": env,
        "stdin_b64": None,
        "effects_path": None,
    }
    frame = json.dumps(payload, allow_nan=False).encode("utf-8")
    return ((backend.helper_path, "exec"), workspace, env, len(frame).to_bytes(8, "big") + frame)


def with_stdin(frame: bytes, stdin: bytes) -> bytes:
    """Merge caller stdin bytes into a framed plan payload (stdin_b64), re-frame."""
    payload = json.loads(frame[8:].decode("utf-8"))
    payload["stdin_b64"] = base64.b64encode(stdin).decode("ascii")
    raw = json.dumps(payload).encode("utf-8")
    return len(raw).to_bytes(8, "big") + raw


def with_effects_path(frame: bytes, effects_path: str) -> bytes:
    """Point the plan payload at an explicit supervisor sidecar, re-frame."""
    payload = json.loads(frame[8:].decode("utf-8"))
    payload["effects_path"] = effects_path
    raw = json.dumps(payload).encode("utf-8")
    return len(raw).to_bytes(8, "big") + raw


def scratch_dir(workspace: str) -> str:
    """Stable runtime-owned scratch, derived from the anchor (never the user cwd)."""
    digest = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:16]
    return str(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"voidcode-sandbox-{digest}")


def read_effects_file(path: Path, *, backend: str) -> ObservedEffects | None:
    """Decode one supervisor sidecar; None when missing or malformed (honest empty)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if isinstance(raw, dict) and not raw.get("backend"):
        raw = {**raw, "backend": backend}
    try:
        return ObservedEffects.from_payload(raw)
    except ValueError:
        return None


def _child_env(baseline: dict[str, str], overrides: dict[str, str], *, scratch: str, cwd: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _ENV_BASELINE_KEYS:
        value = baseline.get(key)
        if value is not None:
            env[key] = value
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("TERM", "xterm-256color")
    env["TMPDIR"] = scratch
    env["HOME"] = cwd
    for key, value in overrides.items():
        if key in _DROPPED_ENV_KEYS or key == "PYTHONSTARTUP" or (key.startswith("PYTHON") and key != "PYTHONIOENCODING"):
            continue
        env[key] = value
    return env
