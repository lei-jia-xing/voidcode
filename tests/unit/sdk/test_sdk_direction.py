from __future__ import annotations

import ast
import sys
from pathlib import Path


def test_sdk_cannot_depend_on_implementation_layers() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "voidcode" / "sdk"
    assert root.is_dir(), f"SDK directory missing: {root}"
    sources = tuple(root.rglob("*.py"))
    assert sources, "SDK guard did not discover any source files"
    violations: list[str] = []
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            targets: tuple[str, ...] = ()
            if isinstance(node, ast.ImportFrom):
                if node.level > 1:
                    violations.append(f"{source.name}:{node.lineno}: escaped relative import")
                elif node.level == 0 and node.module:
                    targets = (node.module,)
            elif isinstance(node, ast.Import):
                targets = tuple(alias.name for alias in node.names)
            for target in targets:
                if target == "voidcode.sdk" or target.startswith("voidcode.sdk."):
                    continue
                top = target.partition(".")[0]
                if top not in sys.stdlib_module_names and top not in {"pydantic", "jsonschema"}:
                    violations.append(f"{source.name}: prohibited SDK dependency {target}")
    assert not violations, "\n".join(violations)


def test_semantic_consumers_use_public_facts_instead_of_private_event_parsers() -> None:
    plugins = Path(__file__).resolve().parents[3] / "src" / "voidcode" / "plugins"
    violations: list[str] = []
    for name in (
        "effectiveness/effectiveness_plugin.py",
        "skills/skills_plugin.py",
        "todo/todo_plugin.py",
        "cron/cron_plugin.py",
        "compaction/compaction_plugin.py",
        "compaction/projection.py",
    ):
        for node in ast.walk(ast.parse((plugins / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "read_events":
                violations.append(f"{name}:{node.lineno}: private event read")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("kernel."):
                violations.append(f"{name}:{node.lineno}: private event name")
    assert not violations, "\n".join(violations)


def test_provider_panels_use_public_transcript_instead_of_private_events() -> None:
    source = Path(__file__).resolve().parents[3] / "src/voidcode/capabilities/providers/panels.py"
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute):
            assert node.attr != "read_events"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith("kernel.")
