"""Formatter plugin: post-write formatting as an in-invocation hook.

Ports the legacy formatter policy (``src/voidcode/formatter/{config,executor}.py`` plus the
post-write trigger sites in ``tools/write.py``/``tools/edit.py``) onto the kernel's post-tool
hook seam:

- the registration declares one ``post_tool`` hook with ``mutating=True`` for the file-mutating
  tools, so the kernel runs it inside the SAME governed invocation, only after a successful
  result, and appends its note to the result the model reads and the events record;
- a restrictive stance (frozen read-only or an active overlay) makes the kernel SKIP the hook
  and say so in the result instead of formatting - the plugin never gets to widen policy;
- only warnings are surfaced, exactly like the legacy tool output (" Formatter warning: ..."):
  a successful format is visible in the file itself, not as an extra sentence;
- formatting never fails a tool call: a missing binary, a non-zero exit or a timeout becomes a
  warning note, never an error verdict for the write that already succeeded.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.hooks import ToolOutcome
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import PluginServices

from .formatter_config import (
    FormatterRuntimeConfig,
    RuntimeFormatterPresetConfig,
    apply_formatter_overrides,
    default_formatter_presets,
)
from .formatter_executor import FormatterExecutor, formatter_diagnostics

#: Tools whose successful result is offered to this hook. ``apply_patch`` names no registered
#: tool (patch runs inside ``edit``), so it matches nothing; kept for forward compatibility.
FORMATTED_TOOL_NAMES: tuple[str, ...] = ("write", "edit", "apply_workspace_edit", "apply_patch", "ast_grep")
#: Argument keys the file tools use for the written path.
_PATH_ARGUMENT_KEYS: tuple[str, ...] = ("path", "file_path", "target_path")
_CWD_POLICIES: frozenset[str] = frozenset({"workspace", "file_directory", "nearest_root"})


def _optional_bool(raw: Mapping[str, object], key: str) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"formatter config field '{key}' must be a boolean")
    return value


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"formatter config field '{field}' must be an array of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"formatter config field '{field}' must contain non-empty strings")
        items.append(item)
    return tuple(items)


def _command_tuple(value: object, *, field: str) -> tuple[str, ...]:
    commands = _string_tuple(value, field=field)
    if not commands:
        raise ValueError(f"formatter config field '{field}' must not be empty")
    return commands


def _parse_preset(suffix: str, raw: object) -> RuntimeFormatterPresetConfig:
    if not isinstance(raw, Mapping):
        raise ValueError(f"formatter language '{suffix}' must be an object")
    unknown = sorted(str(key) for key in raw if key not in {"command", "extensions", "root_markers", "fallbacks", "cwd"})
    if unknown:
        raise ValueError(f"formatter language '{suffix}' field '{unknown[0]}' is not supported")
    command = _command_tuple(raw.get("command"), field=f"languages.{suffix}.command")
    extensions = (suffix,) if raw.get("extensions") is None else _string_tuple(raw.get("extensions"), field=f"languages.{suffix}.extensions")
    root_markers = () if raw.get("root_markers") is None else _string_tuple(raw.get("root_markers"), field=f"languages.{suffix}.root_markers")
    fallbacks_raw = raw.get("fallbacks")
    fallbacks: tuple[tuple[str, ...], ...] = ()
    if fallbacks_raw is not None:
        if not isinstance(fallbacks_raw, (list, tuple)):
            raise ValueError(f"formatter language '{suffix}' field 'fallbacks' must be an array")
        fallbacks = tuple(_command_tuple(entry, field=f"languages.{suffix}.fallbacks") for entry in fallbacks_raw)
    cwd = raw.get("cwd", "nearest_root")
    if not isinstance(cwd, str) or cwd not in _CWD_POLICIES:
        raise ValueError(f"formatter language '{suffix}' field 'cwd' must be one of {sorted(_CWD_POLICIES)}")
    return RuntimeFormatterPresetConfig(
        command=command,
        extensions=extensions,
        root_markers=root_markers,
        fallback_commands=fallbacks,
        cwd_policy=cwd,  # type: ignore[arg-type]
    )


def parse_formatter_config(config: Mapping[str, object] | None) -> FormatterRuntimeConfig:
    """Merge the plugin's config over the builtin presets, mirroring the legacy merge."""
    base = FormatterRuntimeConfig(enabled=True, format_on_write=False, formatter_presets=default_formatter_presets())
    if config is None:
        return base
    unknown = sorted(str(key) for key in config if key not in {"enabled", "format_on_write", "languages", "providers"})
    if unknown:
        raise ValueError(f"formatter config field '{unknown[0]}' is not supported")
    raw_languages = config.get("languages")
    languages: dict[str, RuntimeFormatterPresetConfig] = {}
    if raw_languages is not None:
        if not isinstance(raw_languages, Mapping):
            raise ValueError("formatter config field 'languages' must be an object")
        for suffix, raw_preset in raw_languages.items():
            if not isinstance(suffix, str) or not suffix.startswith("."):
                raise ValueError("formatter config language keys must be file suffixes like '.py'")
            languages[suffix] = _parse_preset(suffix, raw_preset)
    return apply_formatter_overrides(
        base,
        enabled=_optional_bool(config, "enabled"),
        format_on_write=_optional_bool(config, "format_on_write"),
        languages=languages or None,
    )


def _target_path(outcome: ToolOutcome, workspace: Path) -> Path | None:
    raw_path = next((outcome.arguments.get(key) for key in _PATH_ARGUMENT_KEYS if isinstance(outcome.arguments.get(key), str)), None)
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path)
    resolved = (workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not resolved.is_relative_to(workspace):
        return None
    return resolved


class FormatterHook:
    """Run the configured formatter on a successfully written file and note warnings."""

    def __init__(self, config: FormatterRuntimeConfig) -> None:
        self._config = config

    def __call__(self, outcome: ToolOutcome, context: ToolContext, services: PluginServices) -> str | None:
        _ = services
        if outcome.status != "ok":
            return None
        workspace = context.require_workspace().resolve()
        target = _target_path(outcome, workspace)
        if target is None:
            return None
        result = FormatterExecutor(self._config, workspace).run(target)
        if result.status == "not_configured":
            return None
        diagnostics = formatter_diagnostics(result)
        warnings = [str(entry.get("message", "")) for entry in diagnostics if entry.get("message")]
        if warnings:
            return f"Formatter warning: {warnings[0]}"
        return None


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    formatter_config = parse_formatter_config(config)
    return Plugin(
        post_tool=FormatterHook(formatter_config),
        post_tool_match=("write", "edit", "apply_workspace_edit", "apply_patch", "ast_grep"),
        post_tool_mutating=True,
    )
