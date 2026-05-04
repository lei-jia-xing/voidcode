from __future__ import annotations

from .contracts import LspServerPreset

_PYTHON_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", ".git")
_NODE_ROOT_MARKERS = ("package.json", ".git")
_RUST_ROOT_MARKERS = ("Cargo.toml", "rust-project.json", ".git")
_GO_ROOT_MARKERS = ("go.work", "go.mod", ".git")
_C_CPP_ROOT_MARKERS = ("compile_commands.json", "compile_flags.txt", ".clangd", ".git")
_JAVA_ROOT_MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", ".git")
_DOTNET_ROOT_MARKERS = ("global.json", "Directory.Build.props", ".git")
_RUBY_ROOT_MARKERS = ("Gemfile", ".ruby-version", ".git")
_PHP_ROOT_MARKERS = ("composer.json", ".git")
_LUA_ROOT_MARKERS = (".luarc.json", ".luarc.jsonc", ".git")
_WEB_ROOT_MARKERS = ("package.json", "tsconfig.json", "jsconfig.json", ".git")
_TAILWIND_ROOT_MARKERS = (
    "tailwind.config.js",
    "tailwind.config.cjs",
    "tailwind.config.mjs",
    "tailwind.config.ts",
    "tailwind.config.cts",
    "tailwind.config.mts",
    "package.json",
    ".git",
)
_DOC_ROOT_MARKERS = (".git",)
_XML_ROOT_MARKERS = ("pom.xml", "build.xml", ".git")
_TOML_ROOT_MARKERS = ("pyproject.toml", "Cargo.toml", "taplo.toml", ".git")
_KOTLIN_ROOT_MARKERS = ("build.gradle.kts", "settings.gradle.kts", "pom.xml", ".git")
_BASH_ROOT_MARKERS = (".git",)
_DOCKER_ROOT_MARKERS = ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", ".git")
_CMAKE_ROOT_MARKERS = ("CMakeLists.txt", ".git")
_ZIG_ROOT_MARKERS = ("build.zig", "zls.json", ".git")
_TEX_ROOT_MARKERS = ("latexmkrc", ".git")

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
        id="bashls",
        command=("bash-language-server", "start"),
        extensions=(".sh", ".bash", ".zsh"),
        languages=("bash", "shell", "zsh"),
        root_markers=_BASH_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="clangd",
        command=("clangd",),
        extensions=(".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"),
        languages=("c", "cpp", "objective-c", "objective-cpp"),
        root_markers=_C_CPP_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="cmake",
        command=("cmake-language-server",),
        extensions=(".cmake",),
        languages=("cmake",),
        root_markers=_CMAKE_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="cssls",
        command=("vscode-css-language-server", "--stdio"),
        extensions=(".css", ".scss", ".less"),
        languages=("css", "scss", "less"),
        root_markers=_WEB_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="dockerls",
        command=("docker-langserver", "--stdio"),
        extensions=(".dockerfile",),
        languages=("dockerfile",),
        root_markers=_DOCKER_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="docker-compose-language-service",
        command=("docker-compose-langserver", "--stdio"),
        extensions=(".yml", ".yaml"),
        languages=("dockercompose", "yaml"),
        root_markers=("docker-compose.yml", "docker-compose.yaml", ".git"),
    ),
    LspServerPreset(
        id="eslint",
        command=("vscode-eslint-language-server", "--stdio"),
        extensions=(".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
        languages=("javascript", "javascriptreact", "typescript", "typescriptreact"),
        root_markers=_NODE_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="gopls",
        command=("gopls",),
        extensions=(".go",),
        languages=("go",),
        root_markers=_GO_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="html",
        command=("vscode-html-language-server", "--stdio"),
        extensions=(".html", ".htm"),
        languages=("html",),
        root_markers=_WEB_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="jsonls",
        command=("vscode-json-language-server", "--stdio"),
        extensions=(".json", ".jsonc"),
        languages=("json", "jsonc"),
        root_markers=_DOC_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="jdtls",
        command=("jdtls",),
        extensions=(".java",),
        languages=("java",),
        root_markers=_JAVA_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="kotlin-language-server",
        command=("kotlin-language-server",),
        extensions=(".kt", ".kts"),
        languages=("kotlin",),
        root_markers=_KOTLIN_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="lemminx",
        command=("lemminx",),
        extensions=(".xml", ".xsd", ".xsl", ".xslt", ".svg"),
        languages=("xml", "xsd", "xsl", "xslt", "svg"),
        root_markers=_XML_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="lua_ls",
        command=("lua-language-server",),
        extensions=(".lua",),
        languages=("lua",),
        root_markers=_LUA_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="marksman",
        command=("marksman", "server"),
        extensions=(".md", ".markdown", ".mdx"),
        languages=("markdown", "mdx"),
        root_markers=_DOC_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="intelephense",
        command=("intelephense", "--stdio"),
        extensions=(".php", ".phtml"),
        languages=("php",),
        root_markers=_PHP_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="phpactor",
        command=("phpactor", "language-server"),
        extensions=(".php", ".phtml"),
        languages=("php",),
        root_markers=_PHP_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="ruby-lsp",
        command=("ruby-lsp",),
        extensions=(".rb", ".rake", ".gemspec"),
        languages=("ruby",),
        root_markers=_RUBY_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="solargraph",
        command=("solargraph", "stdio"),
        extensions=(".rb", ".rake", ".gemspec"),
        languages=("ruby",),
        root_markers=_RUBY_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="rust-analyzer",
        command=("rust-analyzer",),
        extensions=(".rs",),
        languages=("rust",),
        root_markers=_RUST_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="taplo",
        command=("taplo", "lsp", "stdio"),
        extensions=(".toml",),
        languages=("toml",),
        root_markers=_TOML_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="texlab",
        command=("texlab",),
        extensions=(".tex", ".bib"),
        languages=("tex", "bibtex"),
        root_markers=_TEX_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="tailwindcss",
        command=("tailwindcss-language-server", "--stdio"),
        extensions=(
            ".astro",
            ".blade.php",
            ".css",
            ".heex",
            ".html",
            ".js",
            ".jsx",
            ".less",
            ".md",
            ".mdx",
            ".php",
            ".scss",
            ".svelte",
            ".ts",
            ".tsx",
            ".vue",
        ),
        languages=(
            "astro",
            "blade",
            "css",
            "heex",
            "html",
            "javascript",
            "javascriptreact",
            "less",
            "markdown",
            "mdx",
            "php",
            "scss",
            "svelte",
            "tailwindcss",
            "typescript",
            "typescriptreact",
            "vue",
        ),
        root_markers=_TAILWIND_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="tsserver",
        command=("typescript-language-server", "--stdio"),
        extensions=(".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"),
        languages=("typescript", "javascript"),
        root_markers=("tsconfig.json", "jsconfig.json", "package.json", ".git"),
    ),
    LspServerPreset(
        id="vue-language-server",
        command=("vue-language-server", "--stdio"),
        extensions=(".vue",),
        languages=("vue",),
        root_markers=_WEB_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="yamlls",
        command=("yaml-language-server", "--stdio"),
        extensions=(".yaml", ".yml"),
        languages=("yaml",),
        root_markers=_DOC_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="zls",
        command=("zls",),
        extensions=(".zig", ".zon"),
        languages=("zig",),
        root_markers=_ZIG_ROOT_MARKERS,
    ),
    LspServerPreset(
        id="csharp-ls",
        command=("csharp-ls",),
        extensions=(".cs", ".csx"),
        languages=("csharp",),
        root_markers=_DOTNET_ROOT_MARKERS,
    ),
)

_BUILTIN_LSP_SERVER_PRESET_MAP = {preset.id: preset for preset in _BUILTIN_LSP_SERVER_PRESETS}


def builtin_lsp_server_presets() -> tuple[LspServerPreset, ...]:
    return _BUILTIN_LSP_SERVER_PRESETS


def get_builtin_lsp_server_preset(server_id: str) -> LspServerPreset | None:
    return _BUILTIN_LSP_SERVER_PRESET_MAP.get(server_id)


def has_builtin_lsp_server_preset(server_id: str) -> bool:
    return server_id in _BUILTIN_LSP_SERVER_PRESET_MAP
