"""Tests for the capability doctor checker module."""

from __future__ import annotations

from voidcode.doctor.checker import (
    CapabilityCheckStatus,
    DoctorCheckType,
    ExecutableChecker,
    FormatterPresetChecker,
    LspServerChecker,
    McpServerChecker,
)


class TestExecutableChecker:
    """Tests for the ExecutableChecker class."""

    def test_check_finds_existing_executable(self) -> None:
        """Test that the checker finds an executable that exists in PATH."""
        checker = ExecutableChecker("python", "python")
        result = checker.check()

        assert result.status == CapabilityCheckStatus.READY
        assert result.name == "python"
        assert result.check_type == DoctorCheckType.EXECUTABLE.value
        assert "command" in result.details

    def test_check_not_found_for_missing_executable(self) -> None:
        """Test that the checker reports not found for missing executables."""
        checker = ExecutableChecker(
            "definitely-not-real-tool-xyz123",
            "definitely-not-real-tool-xyz123",
        )
        result = checker.check()

        assert result.status == CapabilityCheckStatus.NOT_FOUND
        assert result.name == "definitely-not-real-tool-xyz123"
        assert result.error_message is not None
        assert "not found" in result.error_message.lower()

    def test_check_tries_multiple_commands(self) -> None:
        """Test that the checker tries multiple command names."""
        checker = ExecutableChecker("python3", "python3", "python")
        result = checker.check()

        # python exists, so this should succeed
        assert result.status == CapabilityCheckStatus.READY
        assert "command" in result.details


class TestFormatterPresetChecker:
    """Tests for the FormatterPresetChecker class."""

    def test_check_with_available_formatter(self) -> None:
        """Test checker with an available formatter preset."""
        from voidcode.hook.config import RuntimeFormatterPresetConfig

        preset = RuntimeFormatterPresetConfig(
            command=("python", "--version"),
            extensions=(".py",),
        )
        checker = FormatterPresetChecker("python", preset)
        result = checker.check()

        assert result.status == CapabilityCheckStatus.READY
        assert result.name == "formatter:python"
        assert result.check_type == DoctorCheckType.FORMATTER_PRESET.value

    def test_check_with_missing_formatter(self) -> None:
        """Test checker with a missing formatter preset."""
        from voidcode.hook.config import RuntimeFormatterPresetConfig

        preset = RuntimeFormatterPresetConfig(
            command=("definitely-not-a-formatter-xyz",),
            extensions=(".xyz",),
        )
        checker = FormatterPresetChecker("custom", preset)
        result = checker.check()

        assert result.status == CapabilityCheckStatus.NOT_FOUND
        assert result.error_message is not None


class TestLspServerChecker:
    """Tests for the LspServerChecker class."""

    def test_check_with_available_lsp(self) -> None:
        """Test checker with an available LSP server."""
        from voidcode.lsp.presets import LspServerPreset

        preset = LspServerPreset(
            id="test-lsp",
            command=("python", "--version"),
            extensions=(".py",),
            languages=("python",),
        )
        checker = LspServerChecker("test-lsp", preset)
        result = checker.check()

        # Python is available, so this should succeed
        assert result.status == CapabilityCheckStatus.READY
        assert result.name == "lsp:test-lsp"
        assert result.check_type == DoctorCheckType.LSP_SERVER.value
        assert result.details.get("preset_id") is None

    def test_check_with_alias_lsp_preserves_preset_id(self) -> None:
        """Test checker keeps preset id for alias-style builtin mappings."""
        from voidcode.lsp.presets import LspServerPreset

        preset = LspServerPreset(
            id="pyright",
            command=("python", "--version"),
            extensions=(".py",),
            languages=("python",),
        )
        checker = LspServerChecker("python", preset)
        result = checker.check()

        assert result.status == CapabilityCheckStatus.READY
        assert result.details.get("preset_id") == "pyright"

    def test_check_with_missing_lsp(self) -> None:
        """Test checker with a missing LSP server."""
        from voidcode.lsp.presets import LspServerPreset

        preset = LspServerPreset(
            id="missing-lsp",
            command=("missing-lsp-binary-xyz",),
            extensions=(".xyz",),
        )
        checker = LspServerChecker("missing-lsp", preset)
        result = checker.check()

        assert result.status == CapabilityCheckStatus.NOT_FOUND
        assert result.error_message is not None

    def test_check_with_no_command(self) -> None:
        """Test checker with LSP preset that has no command."""
        from voidcode.lsp.presets import LspServerPreset

        preset = LspServerPreset(
            id="no-command",
            command=(),
            extensions=(".py",),
        )
        checker = LspServerChecker("no-command", preset)
        result = checker.check()

        assert result.status == CapabilityCheckStatus.NOT_CONFIGURED
        assert result.error_message is not None


class TestMcpServerChecker:
    """Tests for the McpServerChecker class."""

    def test_check_with_available_mcp_server(self) -> None:
        """Test checker with an available MCP server."""
        from voidcode.mcp.config import McpServerConfig

        config = McpServerConfig(
            command=("python", "--version"),
            transport="stdio",
        )
        checker = McpServerChecker("test-mcp", config)
        result = checker.check()

        # Python is available, so this should succeed
        assert result.status == CapabilityCheckStatus.READY
        assert result.name == "mcp:test-mcp"
        assert result.check_type == DoctorCheckType.MCP_SERVER.value

    def test_check_with_missing_mcp_server(self) -> None:
        """Test checker with a missing MCP server."""
        from voidcode.mcp.config import McpServerConfig

        config = McpServerConfig(
            command=("missing-mcp-server-xyz",),
            transport="stdio",
        )
        checker = McpServerChecker("missing-mcp", config)
        result = checker.check()

        assert result.status == CapabilityCheckStatus.NOT_FOUND
        assert result.error_message is not None

    def test_check_with_no_command(self) -> None:
        """Test checker with MCP config that has no command."""
        from voidcode.mcp.config import McpServerConfig

        config = McpServerConfig(
            command=(),
            transport="stdio",
        )
        checker = McpServerChecker("no-command", config)
        result = checker.check()

        assert result.status == CapabilityCheckStatus.NOT_CONFIGURED
        assert result.error_message is not None
