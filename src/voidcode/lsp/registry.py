from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

from .contracts import LspServerConfigOverride, LspServerPreset, ResolvedLspServerConfig
from .presets import get_builtin_lsp_server_preset, has_builtin_lsp_server_preset


def resolve_lsp_server_config(
    server_name: str,
    override: LspServerConfigOverride | None,
) -> ResolvedLspServerConfig:
    effective_override = override or LspServerConfigOverride()

    if effective_override.preset is not None and not has_builtin_lsp_server_preset(
        effective_override.preset
    ):
        raise ValueError(f"unknown LSP preset: {effective_override.preset}")

    preset = _resolve_preset(server_name=server_name, override=effective_override)
    command = effective_override.command or (preset.command if preset is not None else ())
    if not command:
        raise ValueError(
            f"LSP server '{server_name}' must define a command or reference a known preset"
        )

    return ResolvedLspServerConfig(
        id=server_name,
        preset=preset.id if preset is not None else None,
        command=command,
        extensions=_merge_string_sequences(
            _normalize_extensions(preset.extensions if preset is not None else ()),
            _normalize_extensions(effective_override.extensions),
        ),
        languages=_merge_string_sequences(
            _normalize_names(preset.languages if preset is not None else ()),
            _normalize_names(effective_override.languages),
        ),
        root_markers=_merge_string_sequences(
            preset.root_markers if preset is not None else (),
            effective_override.root_markers,
        ),
        settings=_deep_merge_dicts(
            preset.settings if preset is not None else {},
            effective_override.settings,
        ),
        init_options=_deep_merge_dicts(
            preset.init_options if preset is not None else {},
            effective_override.init_options,
        ),
    )


def resolve_lsp_server_configs(
    servers: Mapping[str, LspServerConfigOverride] | None,
) -> dict[str, ResolvedLspServerConfig]:
    if not servers:
        return {}
    return {
        server_name: resolve_lsp_server_config(server_name, override)
        for server_name, override in servers.items()
    }


def match_lsp_servers_for_path(
    servers: Mapping[str, ResolvedLspServerConfig],
    file_path: Path,
) -> tuple[str, ...]:
    return tuple(
        server_name for server_name, config in servers.items() if config.matches_path(file_path)
    )


def _resolve_preset(
    *,
    server_name: str,
    override: LspServerConfigOverride,
) -> LspServerPreset | None:
    if override.preset is not None:
        return get_builtin_lsp_server_preset(override.preset)
    if has_builtin_lsp_server_preset(server_name):
        return get_builtin_lsp_server_preset(server_name)
    return None


def _merge_string_sequences(base: Iterable[str], extra: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in (*tuple(base), *tuple(extra)):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return tuple(merged)


def _normalize_extensions(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized_value = value.lower()
        if not normalized_value.startswith("."):
            normalized_value = f".{normalized_value}"
        if normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized.append(normalized_value)
    return tuple(normalized)


def _normalize_names(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized_value = value.lower()
        if normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized.append(normalized_value)
    return tuple(normalized)


def _deep_merge_dicts(
    base: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    merged = {key: value for key, value in base.items()}
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(
                cast(dict[str, object], current),
                cast(dict[str, object], value),
            )
            continue
        merged[key] = value
    return merged
