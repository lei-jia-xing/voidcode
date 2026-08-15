from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: Lowercase file extension (without leading dot) -> tree-sitter-language-pack language id.
#: ``jsx`` is intentionally mapped to ``javascript``: the bundled javascript grammar
#: parses JSX, and the language pack has no separate jsx grammar (requesting one would
#: attempt a download).
_EXTENSION_LANGUAGES: dict[str, str] = {
    "py": "python",
    "pyi": "python",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "mts": "typescript",
    "cts": "typescript",
    "tsx": "tsx",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "sh": "bash",
    "bash": "bash",
    "zsh": "bash",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "hh": "cpp",
    "cs": "c_sharp",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "rb": "ruby",
    "php": "php",
    "sql": "sql",
    "html": "html",
    "htm": "html",
    "css": "css",
    "scss": "scss",
    "md": "markdown",
    "markdown": "markdown",
    "lua": "lua",
    "r": "r",
    "dart": "dart",
    "ex": "elixir",
    "exs": "elixir",
    "erl": "erlang",
    "hrl": "erlang",
    "hs": "haskell",
    "ml": "ocaml",
    "mli": "ocaml",
    "fs": "fsharp",
    "fsi": "fsharp",
    "scala": "scala",
    "swift": "swift",
    "kt": "kotlin",
    "kts": "kotlin",
    "vue": "vue",
    "svelte": "svelte",
    "sol": "solidity",
}

#: Env var gate: ``VOIDCODE_SYNTAX_VALIDATION=0`` (or false/no/off) disables
#: tree-sitter validation; any other value (or absence) leaves it enabled.
_SYNTAX_VALIDATION_ENV = "VOIDCODE_SYNTAX_VALIDATION"
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

_MAX_ERROR_SNIPPET = 100


def language_for_path(path: Path) -> str | None:
    """Return the tree-sitter language id for ``path``'s extension, or ``None``.

    Detection is purely extension-based and independent of the (optional)
    tree-sitter install, so callers can branch before importing anything.
    """
    return _EXTENSION_LANGUAGES.get(path.suffix.lower().lstrip("."))


def syntax_validation_enabled() -> bool:
    """Return whether tree-sitter post-edit validation is enabled.

    Controlled by the ``VOIDCODE_SYNTAX_VALIDATION`` env var; enabled by default.
    """
    value = os.environ.get(_SYNTAX_VALIDATION_ENV, "1").strip().lower()
    return value not in _DISABLED_VALUES


def _display_path(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _parse_with_tree_sitter(language: str, text: str) -> Any | None:
    """Parse ``text`` with tree-sitter for ``language``; ``None`` when unavailable."""
    import tree_sitter_language_pack as lang_pack

    has_language = getattr(lang_pack, "has_language", None)
    if has_language is not None and not has_language(language):
        return None
    parser = lang_pack.get_parser(language)
    return parser.parse(text.encode("utf-8"))


def _collect_error_nodes(node: Any, errors: list[Any]) -> None:
    """Collect every ERROR / missing node in the subtree, depth-first."""
    if node.type == "ERROR" or node.is_missing:
        errors.append(node)
    for child in node.children:
        _collect_error_nodes(child, errors)


def _node_message(node: Any) -> str:
    if node.is_missing:
        return f"Missing {node.type}"
    raw = node.text if node.text is not None else b""
    snippet = " ".join(raw.decode("utf-8", errors="replace").split())
    if not snippet:
        return "Syntax error"
    if len(snippet) > _MAX_ERROR_SNIPPET:
        snippet = f"{snippet[: _MAX_ERROR_SNIPPET - 3]}..."
    return f'Syntax error near "{snippet}"'


def syntax_diagnostics_for_file(*, path: Path, workspace: Path) -> list[dict[str, object]]:
    """Return tree-sitter syntax diagnostics for ``path``, or ``[]`` when unavailable.

    Never raises: missing optional dependency, unknown language, unreadable or
    non-UTF-8 content, and parse failures all degrade to ``[]``. Diagnostics use
    the LSP convention of 1-based ``line`` / ``character``.
    """
    if not syntax_validation_enabled():
        return []
    language = language_for_path(path)
    if language is None:
        return []

    try:
        content = path.read_bytes()
    except OSError:
        return []
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return []

    try:
        tree = _parse_with_tree_sitter(language, text)
    except Exception:
        return []
    if tree is None or not tree.root_node.has_error:
        return []

    errors: list[Any] = []
    _collect_error_nodes(tree.root_node, errors)
    errors.sort(key=lambda node: (node.start_point.row, node.start_point.column))
    kept: list[Any] = []
    for node in errors:
        if any(_contains(previous, node) for previous in kept):
            continue
        kept.append(node)

    display = _display_path(path, workspace)
    diagnostics: list[dict[str, object]] = []
    for node in kept:
        diagnostics.append(
            {
                "path": display,
                "source": "tree-sitter",
                "severity": "error",
                "message": _node_message(node),
                "line": node.start_point.row + 1,
                "character": node.start_point.column + 1,
            }
        )
    return diagnostics


def _contains(outer: Any, inner: Any) -> bool:
    """True when ``inner``'s byte range is fully inside ``outer``'s."""
    return outer.start_byte <= inner.start_byte and outer.end_byte >= inner.end_byte


def post_edit_syntax_diagnostics(*, workspace: Path, paths: list[str]) -> list[dict[str, object]]:
    """Collect tree-sitter syntax diagnostics for each path (LSP-style wrapper).

    Mirrors :func:`voidcode.tools._post_edit_diagnostics.post_edit_lsp_diagnostics`:
    raw path strings, deduplicated, resolved against ``workspace`` when relative.
    """
    if not syntax_validation_enabled():
        return []
    diagnostics: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_path in paths:
        if raw_path in seen:
            continue
        seen.add(raw_path)
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        diagnostics.extend(syntax_diagnostics_for_file(path=candidate, workspace=workspace))
    return diagnostics


__all__ = [
    "language_for_path",
    "post_edit_syntax_diagnostics",
    "syntax_diagnostics_for_file",
    "syntax_validation_enabled",
]
