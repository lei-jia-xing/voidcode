from __future__ import annotations

import errno
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig

type FormatterExecutionStatus = Literal[
    "not_configured",
    "formatted",
    "failed",
    "missing_executable",
]


@dataclass(frozen=True, slots=True)
class FormatterExecutionResult:
    status: FormatterExecutionStatus
    path: Path
    language: str | None = None
    cwd: Path | None = None
    command: tuple[str, ...] | None = None
    attempted_commands: tuple[tuple[str, ...], ...] = ()
    stdout: str | None = None
    stderr: str | None = None
    error: str | None = None


class FormatterExecutor:
    def __init__(self, hooks_config: RuntimeHooksConfig, workspace: Path) -> None:
        self._hooks = hooks_config
        self._workspace = workspace.resolve()

    def run(self, file_path: Path) -> FormatterExecutionResult:
        resolved = self._hooks.resolve_formatter(file_path)
        if not resolved:
            return FormatterExecutionResult(status="not_configured", path=file_path)

        lang, preset = resolved
        cwd = self._resolve_formatter_cwd(file_path=file_path, preset=preset)
        attempted_commands: list[tuple[str, ...]] = []
        missing_tools: list[str] = []
        failed_attempts: list[tuple[tuple[str, ...], subprocess.CompletedProcess[str]]] = []

        for command_parts in (preset.command, *preset.fallback_commands):
            cmd = (*command_parts, str(file_path))
            attempted_commands.append(cmd)
            try:
                proc = subprocess.run(
                    list(cmd),
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                )
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    missing_tools.append(command_parts[0])
                    continue

                failed_attempts.append(
                    (
                        cmd,
                        subprocess.CompletedProcess(
                            args=list(cmd),
                            returncode=1,
                            stdout="",
                            stderr=str(exc),
                        ),
                    )
                )
                continue

            if proc.returncode == 0:
                return FormatterExecutionResult(
                    status="formatted",
                    path=file_path,
                    language=lang,
                    cwd=cwd,
                    command=cmd,
                    attempted_commands=tuple(attempted_commands),
                )

            failed_attempts.append((cmd, proc))

        if failed_attempts:
            last_cmd, last_proc = failed_attempts[-1]
            stderr = (last_proc.stderr or last_proc.stdout)[:300].strip()
            return FormatterExecutionResult(
                status="failed",
                path=file_path,
                language=lang,
                cwd=cwd,
                command=last_cmd,
                attempted_commands=tuple(attempted_commands),
                stdout=last_proc.stdout,
                stderr=last_proc.stderr,
                error=(
                    f"Format failed for {file_path.name} using preset '{lang}' from {cwd}: "
                    f"{stderr or 'formatter exited with a non-zero status'}"
                ),
            )

        attempted_tool_names = ", ".join(dict.fromkeys(missing_tools))
        return FormatterExecutionResult(
            status="missing_executable",
            path=file_path,
            language=lang,
            cwd=cwd,
            attempted_commands=tuple(attempted_commands),
            error=(
                f"No formatter executable was available for preset '{lang}'. "
                f"Tried: {attempted_tool_names}. Install one of them or override "
                f"hooks.formatter_presets.{lang}.command in .voidcode.json."
            ),
        )

    def _resolve_formatter_cwd(
        self, *, file_path: Path, preset: RuntimeFormatterPresetConfig
    ) -> Path:
        if preset.cwd_policy == "workspace":
            return self._workspace
        if preset.cwd_policy == "file_directory":
            return file_path.parent
        return self._find_nearest_root(file_path=file_path, preset=preset) or self._workspace

    def _find_nearest_root(
        self, *, file_path: Path, preset: RuntimeFormatterPresetConfig
    ) -> Path | None:
        if not preset.root_markers:
            return None

        current = file_path.parent
        while current.is_relative_to(self._workspace):
            if any((current / marker).exists() for marker in preset.root_markers):
                return current
            if current == self._workspace:
                break
            current = current.parent
        return None
