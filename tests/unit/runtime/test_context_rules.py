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


def test_runtime_file_rule_contexts_cap_preserves_nearest_rules(tmp_path: Path) -> None:
    workspace = tmp_path
    nested = workspace
    expected_paths: list[str] = []
    for index in range(6):
        nested.mkdir(exist_ok=True)
        rule_path = nested / "AGENTS.md"
        rule_path.write_text(f"Rules {index}", encoding="utf-8")
        expected_paths.append(rule_path.relative_to(workspace).as_posix())
        nested = nested / f"level_{index}"
    nested.mkdir(parents=True)
    target = nested / "module.py"
    target.write_text("print('ok')\n", encoding="utf-8")

    contexts = runtime_file_rule_contexts(
        workspace=workspace,
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                data={"path": target.relative_to(workspace).as_posix()},
            ),
        ),
        max_rule_files=3,
    )

    assert [context.path for context in contexts] == expected_paths[-3:]
    assert [context.content for context in contexts] == ["Rules 3", "Rules 4", "Rules 5"]


def test_runtime_file_rule_contexts_skip_invalid_utf8_rule_files(tmp_path: Path) -> None:
    workspace = tmp_path
    (workspace / "AGENTS.md").write_bytes(b"\xff\xfe\x00")
    source_dir = workspace / "src"
    source_dir.mkdir()
    (source_dir / "AGENTS.md").write_text("Src rules", encoding="utf-8")
    target = source_dir / "module.py"
    target.write_text("print('ok')\n", encoding="utf-8")

    contexts = runtime_file_rule_contexts(
        workspace=workspace,
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                data={"path": "src/module.py"},
            ),
        ),
    )

    assert [(context.path, context.content) for context in contexts] == [
        ("src/AGENTS.md", "Src rules")
    ]


def test_runtime_file_rule_contexts_extracts_apply_patch_change_paths(
    tmp_path: Path,
) -> None:
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
                tool_name="apply_patch",
                status="ok",
                data={
                    "changes": [
                        {"path": "src/voidcode/runtime/service.py", "status": "M"},
                    ],
                    "count": 1,
                },
            ),
        ),
    )

    assert [(context.path, context.content) for context in contexts] == [
        ("AGENTS.md", "Root rules"),
        ("src/AGENTS.md", "Src rules"),
    ]
