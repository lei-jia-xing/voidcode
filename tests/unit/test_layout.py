"""Import-direction guards: the new world never reaches back for deleted modules.

These are dependency-boundary guards, not layout pins: nothing in the core, the SDK,
or a bundled plugin may import a deleted module or a concrete plugin.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "voidcode"

#: Top-level packages and modules the cutover deleted. A reappearing name means someone
#: resurrected the legacy architecture instead of extending the core or a plugin.
_LEGACY_TOP_LEVEL = frozenset(
    {
        "acp",
        "agent",
        "cli",
        "command",
        "common",
        "doctor",
        "formatter",
        "hook",
        "lsp",
        "mcp",
        "provider",
        "runtime",
        "server",
        "skills",
        "tools",
        "tui",
    }
)

#: The layers that must never depend on a concrete plugin or on the deleted world.
_GUARDED_ROOTS = ("core", "plugins", "sdk", "capabilities")


def _iter_sources(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _imported_modules(source: Path) -> list[str]:
    """Every module an import statement in this file names, absolute or relative."""
    package_parts = source.relative_to(_SRC).parent.parts
    targets: list[str] = []
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    targets.append(node.module)
                continue
            # A relative import resolves against this file's package; more dots than the
            # package has parts reaches outside ``voidcode`` entirely.
            base = package_parts[: len(package_parts) - (node.level - 1)] if node.level > 1 else package_parts
            prefix = "voidcode." + ".".join([part for part in base if part])
            targets.append(f"{prefix}.{node.module}" if node.module else prefix)
    return targets


def test_new_world_never_imports_legacy_modules() -> None:
    violations: list[str] = []
    for root_name in _GUARDED_ROOTS:
        root = _SRC / root_name
        if not root.is_dir():
            continue
        for source in _iter_sources(root):
            for module in _imported_modules(source):
                head = module.split(".")[1] if module.startswith("voidcode.") else None
                if head in _LEGACY_TOP_LEVEL:
                    violations.append(f"{source.relative_to(_SRC)}: imports {module}")
                if root_name == "plugins" and head == "plugins":
                    imported_parts = module.split(".")
                    own_plugin = source.relative_to(_SRC).parts[1]
                    if len(imported_parts) > 2 and imported_parts[2] != own_plugin:
                        violations.append(f"{source.relative_to(_SRC)}: imports concrete peer {module}")
                if root_name == "capabilities" and head in {"core", "plugins"}:
                    violations.append(f"{source.relative_to(_SRC)}: shared capability imports {module}")
                # The core admits plugins by root from disk; importing the
                # bundled plugin package would make one concrete plugin part of the core.
                if root_name == "core" and module.startswith("voidcode.plugins"):
                    violations.append(f"{source.relative_to(_SRC)}: core imports a concrete plugin: {module}")
    assert not violations, "\n".join(violations)
