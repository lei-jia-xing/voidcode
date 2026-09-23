"""Capability doctor checker implementations."""

from __future__ import annotations

import enum
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..formatter import RuntimeFormatterPresetConfig
    from ..lsp.presets import LspServerPreset
    from ..runtime.config import RuntimeMcpServerConfig

from ..mcp.redaction import format_redacted_mcp_command, redact_mcp_command

# Timeout for executable checks
_EXECUTABLE_CHECK_TIMEOUT = 5.0


def _probe_version(command: str, version_args: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Probe ``command`` for a version string.

    Returns ``(version, probe_error)``. ``probe_error`` is set only when no
    probe could run at all (timeout, permission denied, invalid invocation);
    an executable that runs but prints nothing yields ``(None, None)``.
    """
    last_error: str | None = None
    ran = False
    for version_arg in version_args:
        try:
            result = subprocess.run(
                [command, version_arg],
                capture_output=True,
                text=True,
                timeout=_EXECUTABLE_CHECK_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
            last_error = f"'{command}' could not be run: {exc}"
            continue
        ran = True
        if result.returncode == 0:
            version = result.stdout.strip() or result.stderr.strip()
            if version:
                # Truncate long version strings
                return (version[:100] if len(version) > 100 else version), None
    return None, None if ran else last_error


class CapabilityCheckStatus(enum.Enum):
    """Status of a capability check."""

    READY = "ready"
    """Executable/command is available and working."""

    NOT_FOUND = "not_found"
    """Required executable was not found in PATH."""

    ERROR = "error"
    """Check failed with an error (e.g., wrong version, permission denied)."""

    NOT_CONFIGURED = "not_configured"
    """Capability is defined but not configured/enabled."""


@dataclass(frozen=True, slots=True)
class CapabilityCheckResult:
    """Result of a single capability check."""

    status: CapabilityCheckStatus
    name: str
    check_type: str
    details: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


class DoctorCheckType(enum.Enum):
    """Types of capability checks."""

    EXECUTABLE = "executable"
    FORMATTER_PRESET = "formatter_preset"
    LSP_SERVER = "lsp_server"
    MCP_SERVER = "mcp_server"
    RUNTIME_CONFIG = "runtime_config"
    PROVIDER_READINESS = "provider_readiness"


class ExecutableChecker:
    """Check if an executable is available in PATH."""

    def __init__(self, name: str, *commands: str) -> None:
        self._name = name
        self._commands = commands

    def check(self) -> CapabilityCheckResult:
        """Check if any of the executable names are available."""
        for cmd in self._commands:
            if shutil.which(cmd) is not None:
                # Found it, try to get version info
                version_info, probe_error = _probe_version(cmd, ("--version", "-v", "-V", "version"))
                if probe_error is not None:
                    return CapabilityCheckResult(
                        status=CapabilityCheckStatus.ERROR,
                        name=self._name,
                        check_type=DoctorCheckType.EXECUTABLE.value,
                        details={"command": cmd},
                        error_message=probe_error,
                    )
                return CapabilityCheckResult(
                    status=CapabilityCheckStatus.READY,
                    name=self._name,
                    check_type=DoctorCheckType.EXECUTABLE.value,
                    details={"command": cmd, "version_info": version_info},
                )

        # Not found - provide helpful suggestion
        return CapabilityCheckResult(
            status=CapabilityCheckStatus.NOT_FOUND,
            name=self._name,
            check_type=DoctorCheckType.EXECUTABLE.value,
            details={"tried_commands": list(self._commands)},
            error_message=f"'{self._name}' not found. Tried: {', '.join(self._commands)}",
        )


class FormatterPresetChecker:
    """Check formatter preset executables."""

    def __init__(
        self,
        preset_name: str,
        preset: RuntimeFormatterPresetConfig,
        *,
        check_all_commands: bool = False,
    ) -> None:
        self._preset_name = preset_name
        self._preset = preset
        self._check_all_commands = check_all_commands

    def check(self) -> CapabilityCheckResult:
        """Check if the formatter preset's executable(s) are available."""
        all_commands = [self._preset.command, *self._preset.fallback_commands]
        available: list[str] = []
        missing: list[str] = []
        probe_errors: list[str] = []

        for cmd in all_commands:
            executable = cmd[0] if cmd else ""
            if executable and shutil.which(executable) is not None:
                version, probe_error = _probe_version(executable, ("--version", "-v", "-V"))
                if probe_error is not None:
                    probe_errors.append(probe_error)
                    missing.append(executable)
                    continue
                available.append(f"{executable} (version: {version})" if version else executable)
            else:
                missing.append(executable)

        if not available:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.ERROR if probe_errors else CapabilityCheckStatus.NOT_FOUND,
                name=f"formatter:{self._preset_name}",
                check_type=DoctorCheckType.FORMATTER_PRESET.value,
                details={
                    "preset_name": self._preset_name,
                    "extensions": list(self._preset.extensions),
                    "primary_command": list(self._preset.command) if self._preset.command else [],
                    "fallback_commands": [list(cmd) for cmd in self._preset.fallback_commands],
                },
                error_message=(
                    probe_errors[0]
                    if probe_errors
                    else f"Formatter preset '{self._preset_name}' has no available executable. Tried: {', '.join(missing)}"
                ),
            )

        # Some commands are available
        if not missing:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name=f"formatter:{self._preset_name}",
                check_type=DoctorCheckType.FORMATTER_PRESET.value,
                details={
                    "preset_name": self._preset_name,
                    "extensions": list(self._preset.extensions),
                    "available_commands": available,
                    "primary_command": list(self._preset.command) if self._preset.command else [],
                },
            )

        # Partial availability (some fallbacks work)
        return CapabilityCheckResult(
            status=CapabilityCheckStatus.READY,
            name=f"formatter:{self._preset_name}",
            check_type=DoctorCheckType.FORMATTER_PRESET.value,
            details={
                "preset_name": self._preset_name,
                "extensions": list(self._preset.extensions),
                "available_commands": available,
                "missing_fallbacks": missing,
                "note": "Some fallback commands are missing, primary command works",
            },
        )


class LspServerChecker:
    """Check LSP server command availability."""

    def __init__(self, server_name: str, preset: LspServerPreset) -> None:
        self._server_name = server_name
        self._preset = preset

    def check(self) -> CapabilityCheckResult:
        """Check if the LSP server command is available."""
        command = self._preset.command
        if not command:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_CONFIGURED,
                name=f"lsp:{self._server_name}",
                check_type=DoctorCheckType.LSP_SERVER.value,
                details={
                    "server_name": self._server_name,
                    "languages": list(self._preset.languages) if self._preset.languages else [],
                    "extensions": list(self._preset.extensions) if self._preset.extensions else [],
                },
                error_message=f"LSP server '{self._server_name}' has no command configured",
            )

        executable = command[0]
        if not executable:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_CONFIGURED,
                name=f"lsp:{self._server_name}",
                check_type=DoctorCheckType.LSP_SERVER.value,
                details={
                    "server_name": self._server_name,
                },
                error_message=f"LSP server '{self._server_name}' has empty command",
            )

        if shutil.which(executable) is not None:
            preset_id = self._preset.id
            if preset_id == self._server_name:
                preset_id = None
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name=f"lsp:{self._server_name}",
                check_type=DoctorCheckType.LSP_SERVER.value,
                details={
                    "server_name": self._server_name,
                    "preset_id": preset_id,
                    "command": redact_mcp_command(command),
                    "languages": list(self._preset.languages) if self._preset.languages else [],
                    "extensions": list(self._preset.extensions) if self._preset.extensions else [],
                },
            )

        return CapabilityCheckResult(
            status=CapabilityCheckStatus.NOT_FOUND,
            name=f"lsp:{self._server_name}",
            check_type=DoctorCheckType.LSP_SERVER.value,
            details={
                "server_name": self._server_name,
                "command": redact_mcp_command(command),
                "languages": list(self._preset.languages) if self._preset.languages else [],
                "extensions": list(self._preset.extensions) if self._preset.extensions else [],
            },
            error_message=(
                f"LSP server '{self._server_name}' command '{executable}' not found in PATH. Full command: {format_redacted_mcp_command(command)}"
            ),
        )


class McpServerChecker:
    """Check MCP server command availability."""

    def __init__(
        self,
        server_name: str,
        config: RuntimeMcpServerConfig,
    ) -> None:
        self._server_name = server_name
        self._config = config

    def check(self) -> CapabilityCheckResult:
        """Check if the MCP server command is available."""
        command = self._config.command
        if self._config.transport == "remote-http":
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name=f"mcp:{self._server_name}",
                check_type=DoctorCheckType.MCP_SERVER.value,
                details={
                    "server_name": self._server_name,
                    "transport": self._config.transport,
                    "url": self._config.url,
                },
            )
        if not command:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_CONFIGURED,
                name=f"mcp:{self._server_name}",
                check_type=DoctorCheckType.MCP_SERVER.value,
                details={
                    "server_name": self._server_name,
                    "transport": self._config.transport,
                },
                error_message=f"MCP server '{self._server_name}' has no command configured",
            )

        executable = command[0]
        if not executable:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.NOT_CONFIGURED,
                name=f"mcp:{self._server_name}",
                check_type=DoctorCheckType.MCP_SERVER.value,
                details={
                    "server_name": self._server_name,
                },
                error_message=f"MCP server '{self._server_name}' has empty command",
            )

        if shutil.which(executable) is not None:
            return CapabilityCheckResult(
                status=CapabilityCheckStatus.READY,
                name=f"mcp:{self._server_name}",
                check_type=DoctorCheckType.MCP_SERVER.value,
                details={
                    "server_name": self._server_name,
                    "command": redact_mcp_command(command),
                    "transport": self._config.transport,
                    "has_env": bool(self._config.env),
                },
            )

        return CapabilityCheckResult(
            status=CapabilityCheckStatus.NOT_FOUND,
            name=f"mcp:{self._server_name}",
            check_type=DoctorCheckType.MCP_SERVER.value,
            details={
                "server_name": self._server_name,
                "command": redact_mcp_command(command),
                "transport": self._config.transport,
            },
            error_message=(
                f"MCP server '{self._server_name}' command '{executable}' not found in PATH. Full command: {format_redacted_mcp_command(command)}"
            ),
        )
