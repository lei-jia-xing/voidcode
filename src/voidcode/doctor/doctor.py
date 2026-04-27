"""Main CapabilityDoctor class that orchestrates all capability checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .checker import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    DoctorCheck,
    DoctorCheckType,
    ExecutableChecker,
    FormatterPresetChecker,
    LspServerChecker,
    McpServerChecker,
)

if TYPE_CHECKING:
    from ..hook.config import RuntimeHooksConfig

if TYPE_CHECKING:
    from ..runtime.config import RuntimeConfig


@dataclass
class CapabilityDoctor:
    """Unified capability readiness checker for VoidCode.

    This doctor checks the availability and readiness of external tools
    and capabilities that VoidCode depends on, including:
    - ast-grep binary
    - Formatter presets and their executables
    - LSP server commands
    - MCP server commands

    The doctor is non-blocking and provides structured output suitable
    for both CLI users and agents.
    """

    workspace: Path | None = None
    config: RuntimeConfig | None = None
    _results: list[CapabilityCheckResult] = field(default_factory=list, init=False)
    _checks: list[DoctorCheck] = field(default_factory=list, init=False)

    def add_executable_check(
        self,
        name: str,
        *commands: str,
        description: str = "",
    ) -> None:
        """Add a check for an executable binary.

        Args:
            name: Display name for the capability (e.g., "ast-grep")
            *commands: Possible command names to check in PATH
            description: Human-readable description of what this executable does
        """
        self._checks.append(
            DoctorCheck(
                check_type=DoctorCheckType.EXECUTABLE,
                name=name,
                description=description or f"Check if {name} is available",
            )
        )
        self._results.append(ExecutableChecker(name, *commands).check())

    def add_formatter_preset_check(
        self,
        preset_name: str,
        preset: Any,  # RuntimeFormatterPresetConfig
        description: str = "",
    ) -> None:
        """Add a check for a formatter preset.

        Args:
            preset_name: Name of the formatter preset (e.g., "python", "typescript")
            preset: The RuntimeFormatterPresetConfig object
            description: Human-readable description
        """
        self._checks.append(
            DoctorCheck(
                check_type=DoctorCheckType.FORMATTER_PRESET,
                name=f"formatter:{preset_name}",
                description=description or f"Check formatter preset: {preset_name}",
                data=preset,
            )
        )
        self._results.append(FormatterPresetChecker(preset_name, preset).check())

    def add_lsp_server_check(
        self,
        server_name: str,
        preset: Any,  # LspServerPreset
        description: str = "",
    ) -> None:
        """Add a check for an LSP server.

        Args:
            server_name: Name of the LSP server (e.g., "pyright", "ruff")
            preset: The LspServerPreset object
            description: Human-readable description
        """
        self._checks.append(
            DoctorCheck(
                check_type=DoctorCheckType.LSP_SERVER,
                name=f"lsp:{server_name}",
                description=description or f"Check LSP server: {server_name}",
                data=preset,
            )
        )
        self._results.append(LspServerChecker(server_name, preset).check())

    def add_mcp_server_check(
        self,
        server_name: str,
        config: Any,  # McpServerConfig
        description: str = "",
    ) -> None:
        """Add a check for an MCP server.

        Args:
            server_name: Name of the MCP server
            config: The McpServerConfig object
            description: Human-readable description
        """
        self._checks.append(
            DoctorCheck(
                check_type=DoctorCheckType.MCP_SERVER,
                name=f"mcp:{server_name}",
                description=description or f"Check MCP server: {server_name}",
                data=config,
            )
        )
        self._results.append(McpServerChecker(server_name, config).check())

    def run_all_checks(self) -> list[CapabilityCheckResult]:
        """Run all registered checks and return results.

        Returns:
            List of CapabilityCheckResult objects, one per check.
        """
        return list(self._results)

    def add_result(self, result: CapabilityCheckResult) -> None:
        """Add a precomputed structured check result."""
        self._results.append(result)

    @property
    def results(self) -> list[CapabilityCheckResult]:
        """Get all check results."""
        return list(self._results)

    @property
    def ready_count(self) -> int:
        """Count of checks that are ready."""
        return sum(1 for r in self._results if r.status == CapabilityCheckStatus.READY)

    @property
    def missing_count(self) -> int:
        """Count of checks that are not found."""
        return sum(1 for r in self._results if r.status == CapabilityCheckStatus.NOT_FOUND)

    @property
    def error_count(self) -> int:
        """Count of checks that had errors."""
        return sum(1 for r in self._results if r.status == CapabilityCheckStatus.ERROR)

    @property
    def not_configured_count(self) -> int:
        """Count of checks that are not configured."""
        return sum(1 for r in self._results if r.status == CapabilityCheckStatus.NOT_CONFIGURED)

    def summary(self) -> dict[str, Any]:
        """Get a summary of all check results."""
        return {
            "total": len(self._results),
            "ready": self.ready_count,
            "missing": self.missing_count,
            "errors": self.error_count,
            "not_configured": self.not_configured_count,
        }

    def reset(self) -> None:
        """Reset all checks and results."""
        self._checks.clear()
        self._results.clear()


def _default_hooks_config() -> RuntimeHooksConfig:
    """Return a RuntimeHooksConfig with built-in formatter presets."""
    from ..hook.config import RuntimeHooksConfig

    return RuntimeHooksConfig()


def create_doctor_for_config(
    workspace: Path,
    config: RuntimeConfig,
) -> CapabilityDoctor:
    """Create a CapabilityDoctor pre-populated with checks from runtime config.

    Args:
        workspace: The workspace path
        config: The runtime configuration

    Returns:
        A CapabilityDoctor with all relevant checks registered
    """
    doctor = CapabilityDoctor(workspace=workspace, config=config)

    # Check ast-grep
    doctor.add_executable_check(
        "ast-grep",
        "ast-grep",
        description="Structural code search and replace tool",
    )

    # Check formatter presets.
    # When config.hooks is None, use the built-in defaults (same as runtime does).
    hooks_config = config.hooks if config.hooks is not None else _default_hooks_config()
    # Respect explicit disablement: runtime formatter execution short-circuits
    # when hooks.enabled is False.
    if hooks_config.enabled is not False:
        for preset_name, preset in hooks_config.formatter_presets.items():
            doctor.add_formatter_preset_check(preset_name, preset)

    # Check LSP servers (only when lsp.enabled is True).
    lsp_config = config.lsp
    if lsp_config is not None and lsp_config.enabled is True and lsp_config.servers:
        from ..lsp.presets import get_builtin_lsp_server_preset
        from ..lsp.registry import resolve_lsp_server_config

        for server_name, server_override in lsp_config.servers.items():
            try:
                resolved = resolve_lsp_server_config(server_name, server_override)
                doctor.add_lsp_server_check(
                    server_name,
                    resolved,
                    description=f"LSP server for {server_name}",
                )
            except ValueError:
                # Config-level error for this server; fall back to the builtin preset.
                preset = get_builtin_lsp_server_preset(server_name)
                if preset:
                    doctor.add_lsp_server_check(server_name, preset)

    # Check MCP servers (only when mcp.enabled is True).
    mcp_config = config.mcp
    if mcp_config is not None and mcp_config.enabled is True and mcp_config.servers:
        for server_name, server_config in mcp_config.servers.items():
            doctor.add_mcp_server_check(server_name, server_config)

    if config.execution_engine == "provider" or config.model is not None:
        from ..runtime.service import VoidCodeRuntime

        runtime = VoidCodeRuntime(workspace=workspace, config=config)
        try:
            readiness = runtime.provider_readiness()
        finally:
            exit_method = getattr(runtime, "__exit__", None)
            if callable(exit_method):
                exit_method(None, None, None)
        doctor.add_result(
            CapabilityCheckResult(
                status=(
                    CapabilityCheckStatus.READY
                    if readiness.ok
                    else CapabilityCheckStatus.ERROR
                    if readiness.status == "missing_auth"
                    else CapabilityCheckStatus.NOT_CONFIGURED
                ),
                name="provider.readiness",
                check_type=DoctorCheckType.PROVIDER_READINESS.value,
                details={
                    "provider": readiness.provider,
                    "model": readiness.model,
                    "configured": readiness.configured,
                    "auth_present": readiness.auth_present,
                    "streaming_supported": readiness.streaming_supported,
                    "context_window": readiness.context_window,
                    "max_output_tokens": readiness.max_output_tokens,
                    "fallback_chain": list(readiness.fallback_chain),
                    "status": readiness.status,
                },
                error_message=None if readiness.ok else readiness.guidance,
            )
        )

    return doctor
