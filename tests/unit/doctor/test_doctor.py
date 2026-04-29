"""Tests for the capability doctor module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from voidcode.doctor import (
    CapabilityDoctor,
    create_doctor_for_config,
)
from voidcode.doctor.checker import (
    CapabilityCheckStatus,
    DoctorCheckType,
)
from voidcode.provider.config import OpenAIProviderConfig, ProviderConfigs
from voidcode.runtime.config import RuntimeConfig


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
        config = RuntimeConfig()

        hooks = MagicMock()
        hooks.enabled = True
        hooks.formatter_presets = {}
        with patch("voidcode.doctor.doctor._default_hooks_config", return_value=hooks):
            doctor = create_doctor_for_config(Path("/tmp"), config)

        # Should have at least the ast-grep check
        ast_grep_results = [r for r in doctor.results if "ast-grep" in r.name.lower()]
        assert len(ast_grep_results) >= 1

    def test_handles_none_config_sections(self) -> None:
        """Test that the function handles None config sections gracefully."""
        config = RuntimeConfig()

        hooks = MagicMock()
        hooks.enabled = True
        hooks.formatter_presets = {}
        with patch("voidcode.doctor.doctor._default_hooks_config", return_value=hooks):
            doctor = create_doctor_for_config(Path("/tmp"), config)

        # Should not raise and should have at least ast-grep
        assert len(doctor.results) >= 1

    def test_skips_formatter_checks_when_hooks_disabled(self) -> None:
        """Formatter checks should be skipped when hooks.enabled is False."""
        hooks = MagicMock()
        hooks.enabled = False
        hooks.formatter_presets = {
            "python": MagicMock(),
            "typescript": MagicMock(),
        }

        config = RuntimeConfig(hooks=hooks)

        doctor = create_doctor_for_config(Path("/tmp"), config)

        formatter_results = [
            result
            for result in doctor.results
            if result.check_type == DoctorCheckType.FORMATTER_PRESET.value
        ]
        assert formatter_results == []

        ast_grep_results = [result for result in doctor.results if result.name == "ast-grep"]
        assert len(ast_grep_results) == 1
        assert ast_grep_results[0].status in {
            CapabilityCheckStatus.READY,
            CapabilityCheckStatus.NOT_FOUND,
        }

    def test_skips_provider_readiness_for_deterministic_config_without_model(
        self, tmp_path: Path
    ) -> None:
        config = RuntimeConfig(execution_engine="deterministic")

        doctor = create_doctor_for_config(tmp_path, config)

        readiness_results = [
            result for result in doctor.results if result.name == "provider.readiness"
        ]
        assert readiness_results == []

    def test_adds_provider_readiness_check_for_provider_config(self, tmp_path: Path) -> None:
        config = RuntimeConfig(
            model="openai/gpt-4o",
            execution_engine="provider",
            providers=ProviderConfigs(openai=OpenAIProviderConfig()),
        )

        doctor = create_doctor_for_config(tmp_path, config)

        readiness = next(result for result in doctor.results if result.name == "provider.readiness")
        assert readiness.check_type == DoctorCheckType.PROVIDER_READINESS.value
        assert readiness.status == CapabilityCheckStatus.ERROR
        assert readiness.details["provider"] == "openai"
        assert readiness.details["model"] == "gpt-4o"
        assert readiness.details["auth_present"] is False
        assert readiness.error_message is not None
        assert "openai.api_key" in readiness.error_message

    def test_adds_provider_readiness_check_for_provider_missing_model(self, tmp_path: Path) -> None:
        config = RuntimeConfig(execution_engine="provider")

        doctor = create_doctor_for_config(tmp_path, config)

        readiness = next(result for result in doctor.results if result.name == "provider.readiness")
        assert readiness.check_type == DoctorCheckType.PROVIDER_READINESS.value
        assert readiness.status == CapabilityCheckStatus.ERROR
        assert readiness.details["status"] == "missing_model"
        assert readiness.details["auth_present"] is None
        assert readiness.error_message is not None
        assert "provider/model" in readiness.error_message

    def test_provider_readiness_runtime_error_is_structured_result(self, tmp_path: Path) -> None:
        config = RuntimeConfig(
            model="malformed-model",
            execution_engine="provider",
        )

        doctor = create_doctor_for_config(tmp_path, config)

        readiness = next(result for result in doctor.results if result.name == "provider.readiness")
        assert readiness.check_type == DoctorCheckType.PROVIDER_READINESS.value
        assert readiness.status == CapabilityCheckStatus.ERROR
        assert readiness.details == {
            "model": "malformed-model",
            "execution_engine": "provider",
            "status": "invalid_config",
        }
        assert readiness.error_message is not None
        assert "provider/model" in readiness.error_message

    def test_provider_readiness_runtime_blocker_is_error(self, tmp_path: Path) -> None:
        config = RuntimeConfig(
            model="unknown-provider/model",
            execution_engine="provider",
        )

        doctor = create_doctor_for_config(tmp_path, config)

        readiness = next(result for result in doctor.results if result.name == "provider.readiness")
        assert readiness.status == CapabilityCheckStatus.ERROR
        assert readiness.details["status"] == "invalid_model"
        assert readiness.error_message is not None
        assert "unknown-provider" in readiness.error_message

    def test_provider_readiness_unconfigured_provider_is_error(self, tmp_path: Path) -> None:
        config = RuntimeConfig(
            model="openai/gpt-4o",
            execution_engine="provider",
        )

        doctor = create_doctor_for_config(tmp_path, config)

        readiness = next(result for result in doctor.results if result.name == "provider.readiness")
        assert readiness.status == CapabilityCheckStatus.ERROR
        assert readiness.details["status"] == "unconfigured"
        assert readiness.error_message is not None
        assert "provider credentials" in readiness.error_message
