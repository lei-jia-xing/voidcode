"""Tests for the capability doctor module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from voidcode.doctor import (
    CapabilityDoctor,
    create_doctor_for_config,
)
from voidcode.doctor.checker import (
    DoctorCheckType,
)


class TestCapabilityDoctor:
    """Tests for the CapabilityDoctor class."""

    def test_add_executable_check(self) -> None:
        """Test adding an executable check to the doctor."""
        doctor = CapabilityDoctor()

        # Check for python which should exist
        doctor.add_executable_check(
            "python",
            "python",
            description="Python interpreter",
        )

        assert len(doctor.results) == 1
        result = doctor.results[0]
        assert result.name == "python"
        assert result.check_type == DoctorCheckType.EXECUTABLE.value

    def test_doctor_summary(self) -> None:
        """Test the doctor summary functionality."""
        doctor = CapabilityDoctor()

        # Add some checks
        doctor.add_executable_check("python", "python")
        doctor.add_executable_check("missing-tool", "missing-tool-xyz")

        summary = doctor.summary()
        assert summary["total"] == 2
        assert summary["ready"] >= 1  # Python should be ready
        assert summary["missing"] >= 1  # missing-tool should not be found

    def test_doctor_ready_count(self) -> None:
        """Test the ready count property."""
        doctor = CapabilityDoctor()

        # Add checks
        doctor.add_executable_check("python", "python")
        doctor.add_executable_check("missing", "missing-tool-xyz")

        assert doctor.ready_count >= 1
        assert doctor.missing_count >= 1

    def test_reset_clears_results(self) -> None:
        """Test that reset clears all checks and results."""
        doctor = CapabilityDoctor()
        doctor.add_executable_check("python", "python")

        assert len(doctor.results) > 0

        doctor.reset()

        assert len(doctor.results) == 0


class TestCreateDoctorForConfig:
    """Tests for the create_doctor_for_config function."""

    def test_creates_doctor_with_ast_grep_check(self) -> None:
        """Test that the doctor includes ast-grep check."""
        config = MagicMock()
        config.hooks = None
        config.lsp = None
        config.mcp = None

        doctor = create_doctor_for_config(Path("/tmp"), config)

        # Should have at least the ast-grep check
        ast_grep_results = [r for r in doctor.results if "ast-grep" in r.name.lower()]
        assert len(ast_grep_results) >= 1

    def test_handles_none_config_sections(self) -> None:
        """Test that the function handles None config sections gracefully."""
        config = MagicMock()
        config.hooks = None
        config.lsp = None
        config.mcp = None

        doctor = create_doctor_for_config(Path("/tmp"), config)

        # Should not raise and should have at least ast-grep
        assert len(doctor.results) >= 1
