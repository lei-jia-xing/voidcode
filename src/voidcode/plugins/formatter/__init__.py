"""Formatter plugin package: preset resolution, subprocess execution, and the write hook."""

from .formatter_config import (
    FormatterCwdPolicy,
    FormatterRuntimeConfig,
    RuntimeFormatterPresetConfig,
    apply_formatter_overrides,
    default_formatter_presets,
    resolve_formatter_preset,
)
from .formatter_executor import (
    FORMATTER_TIMEOUT_SECONDS,
    FormatterExecutionResult,
    FormatterExecutionStatus,
    FormatterExecutor,
    formatter_diagnostics,
    formatter_payload,
)
from .formatter_plugin import FormatterHook, create_plugin, parse_formatter_config

__all__ = [
    "FORMATTER_TIMEOUT_SECONDS",
    "FormatterCwdPolicy",
    "FormatterExecutionResult",
    "FormatterExecutionStatus",
    "FormatterExecutor",
    "FormatterHook",
    "FormatterRuntimeConfig",
    "RuntimeFormatterPresetConfig",
    "apply_formatter_overrides",
    "create_plugin",
    "default_formatter_presets",
    "formatter_diagnostics",
    "formatter_payload",
    "parse_formatter_config",
    "resolve_formatter_preset",
]
