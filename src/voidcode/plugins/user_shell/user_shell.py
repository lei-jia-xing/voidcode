"""Login-shell selection and bounded presentation for the governed ``!`` command."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Final

from voidcode.sdk.services import ProcessResult

USER_SHELL_TIMEOUT_SECONDS: Final[int] = 3600


def parse_shell_escape(text: str) -> str | None:
    """Return the command for a ``!``-prefixed submission, else None.

    Detection runs on the trimmed input; the strip is exactly one leading
    ``!`` (``!!foo`` executes ``!foo`` verbatim — there is no ``!!`` form).
    """
    stripped = text.strip()
    if not stripped.startswith("!"):
        return None
    return stripped[1:]


def login_shell_args(command: str) -> list[str]:
    """Build login-shell argv without parsing the command.

    ``bash``/``zsh``/``sh`` get ``-lc``; PowerShell gets ``-Command``; ``cmd``
    gets ``/c``. The command string passes through verbatim — no parsing here.
    """
    path, shell_type = _default_user_shell()
    if shell_type in {"bash", "zsh", "sh"}:
        return [path, "-lc", command]
    if shell_type == "powershell":
        return [path, "-Command", command]
    return [path, "/c", command]


def _default_user_shell() -> tuple[str, str]:
    """Resolve the user’s preferred supported shell, with platform defaults.

    POSIX prefers the user's shell (``$SHELL``), then ``bash``, ``zsh``, and
    finally ``/bin/sh``; Windows prefers PowerShell, then ``cmd``.
    """
    if sys.platform == "win32":
        for candidate in ("pwsh", "powershell"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved, "powershell"
        return "cmd.exe", "cmd"
    user_shell = os.environ.get("SHELL", "")
    name = Path(user_shell).name if user_shell else ""
    if name in {"bash", "zsh", "sh"}:
        resolved = shutil.which(user_shell)
        if resolved:
            return resolved, name
    for name in ("bash", "zsh"):
        resolved = shutil.which(name)
        if resolved:
            return resolved, name
    return "/bin/sh", "sh"


def format_user_shell_record(command: str, result: ProcessResult, output: str) -> str:
    """Describe the actual process outcome without inventing an exit code."""
    return (
        "<user_shell_command>\n"
        f"\n<command>\n{command}\n</command>\n<result>\n"
        f"Exit code: {result.exit_code if result.exit_code is not None else 'unknown'}\n"
        f"Duration: {result.duration_ms / 1000:.3f} seconds\n"
        f"Output:\n{output}\n</result>\n"
        "</user_shell_command>"
    )
