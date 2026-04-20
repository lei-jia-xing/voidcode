from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable, Mapping
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


def derive_workspace_lsp_defaults(
    workspace: Path,
    *,
    executable_exists: Callable[[str], bool] | None = None,
) -> dict[str, LspServerConfigOverride]:
    exists = executable_exists or _default_executable_exists
    derived: dict[str, LspServerConfigOverride] = {}
    for server_name, markers in _SAFE_IMPLICIT_WORKSPACE_DEFAULTS:
        preset = get_builtin_lsp_server_preset(server_name)
        if preset is None:
            continue
        if not _workspace_has_any_marker(workspace, markers):
            continue
        if not exists(preset.command[0]):
            continue
        derived[server_name] = LspServerConfigOverride()
    return derived


def _resolve_preset(
    *,
    server_name: str,
    override: LspServerConfigOverride,
) -> LspServerPreset | None:
    # Builtin server-name lookup is the canonical public path.
    # Explicit preset remains supported for compatibility and alias/custom-name cases.
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


_PYTHON_WORKSPACE_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
)
_TS_WORKSPACE_MARKERS: tuple[str, ...] = ("tsconfig.json", "jsconfig.json", "package.json")
_GO_WORKSPACE_MARKERS: tuple[str, ...] = ("go.work", "go.mod")
_RUST_WORKSPACE_MARKERS: tuple[str, ...] = ("Cargo.toml", "rust-project.json")
_CLANGD_WORKSPACE_MARKERS: tuple[str, ...] = (
    "compile_commands.json",
    "compile_flags.txt",
    ".clangd",
)
_JAVA_WORKSPACE_MARKERS: tuple[str, ...] = (
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
)
_LUA_WORKSPACE_MARKERS: tuple[str, ...] = (".luarc.json", ".luarc.jsonc")
_ZIG_WORKSPACE_MARKERS: tuple[str, ...] = ("build.zig", "zls.json")
_CSHARP_WORKSPACE_MARKERS: tuple[str, ...] = ("global.json", "Directory.Build.props")

_SAFE_IMPLICIT_WORKSPACE_DEFAULTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pyright", _PYTHON_WORKSPACE_MARKERS),
    ("tsserver", _TS_WORKSPACE_MARKERS),
    ("gopls", _GO_WORKSPACE_MARKERS),
    ("rust-analyzer", _RUST_WORKSPACE_MARKERS),
    ("clangd", _CLANGD_WORKSPACE_MARKERS),
    ("jdtls", _JAVA_WORKSPACE_MARKERS),
    ("lua_ls", _LUA_WORKSPACE_MARKERS),
    ("zls", _ZIG_WORKSPACE_MARKERS),
    ("csharp-ls", _CSHARP_WORKSPACE_MARKERS),
)


def _workspace_has_any_marker(workspace: Path, markers: Iterable[str]) -> bool:
    return any((workspace / marker).exists() for marker in markers)


def _default_executable_exists(command: str) -> bool:
    return shutil.which(command) is not None
