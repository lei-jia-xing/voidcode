from __future__ import annotations

from pathlib import Path

from voidcode.runtime.context_rules import runtime_file_rule_contexts
from voidcode.tools.contracts import ToolResult


def test_runtime_file_rule_contexts_load_nearest_workspace_rules(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_text("Root rules", encoding="utf-8")
    nested = workspace / "src" / "voidcode" / "runtime"
    nested.mkdir(parents=True)
    (workspace / "src" / "AGENTS.md").write_text("Src rules", encoding="utf-8")
    (nested / "service.py").write_text("print('ok')\n", encoding="utf-8")

    contexts = runtime_file_rule_contexts(
        workspace=workspace,
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                data={"path": "src/voidcode/runtime/service.py"},
            ),
        ),
    )

    assert [(context.path, context.content) for context in contexts] == [
        ("AGENTS.md", "Root rules"),
        ("src/AGENTS.md", "Src rules"),
    ]


def test_runtime_file_rule_contexts_ignore_external_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Root rules", encoding="utf-8")
    external = tmp_path / "outside.py"
    external.write_text("print('outside')\n", encoding="utf-8")

    contexts = runtime_file_rule_contexts(
        workspace=workspace,
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                data={"path": external.as_posix()},
            ),
        ),
    )

    assert contexts == ()
