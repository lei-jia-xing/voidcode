from __future__ import annotations

from .contracts import LspServerPreset

_PYTHON_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git")

_BUILTIN_LSP_SERVER_PRESETS: tuple[LspServerPreset, ...] = (
    LspServerPreset(
        id="pyright",
        command=("pyright-langserver", "--stdio"),
        extensions=(".py", ".pyi"),
        languages=("python",),
        root_markers=_PYTHON_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="ruff",
        command=("ruff", "server"),
        extensions=(".py", ".pyi"),
        languages=("python",),
        root_markers=_PYTHON_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="gopls",
        command=("gopls",),
        extensions=(".go",),
        languages=("go",),
        root_markers=("go.work", "go.mod", ".git"),
    ),
    LspServerPreset(
        id="rust-analyzer",
        command=("rust-analyzer",),
        extensions=(".rs",),
        languages=("rust",),
        root_markers=("Cargo.toml", "rust-project.json", ".git"),
    ),
    LspServerPreset(
        id="tsserver",
        command=("typescript-language-server", "--stdio"),
        extensions=(".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"),
        languages=("typescript", "javascript"),
        root_markers=("tsconfig.json", "jsconfig.json", "package.json", ".git"),
    ),
)

_BUILTIN_LSP_SERVER_PRESET_MAP = {preset.id: preset for preset in _BUILTIN_LSP_SERVER_PRESETS}


def builtin_lsp_server_presets() -> tuple[LspServerPreset, ...]:
    return _BUILTIN_LSP_SERVER_PRESETS


def get_builtin_lsp_server_preset(server_id: str) -> LspServerPreset | None:
    return _BUILTIN_LSP_SERVER_PRESET_MAP.get(server_id)


def has_builtin_lsp_server_preset(server_id: str) -> bool:
    return server_id in _BUILTIN_LSP_SERVER_PRESET_MAP
