from __future__ import annotations

from pathlib import Path

from voidcode.context import directory_readme_contexts
from voidcode.tools.contracts import ToolResult


def test_directory_readme_contexts_load_root_and_nearest_readmes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Project readme", encoding="utf-8")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "README.md").write_text("Src readme", encoding="utf-8")

    contexts = directory_readme_contexts(
        workspace=tmp_path,
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="content",
                data={"path": "src/app.py", "arguments": {"filePath": "src/app.py"}},
            ),
        ),
    )

    assert [context.path for context in contexts] == ["README.md", "src/README.md"]
    assert contexts[0].content == "Project readme"
    assert contexts[1].content == "Src readme"


def test_directory_readme_contexts_ignore_external_paths(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Project readme", encoding="utf-8")

    contexts = directory_readme_contexts(
        workspace=tmp_path,
        tool_results=(
            ToolResult(
                tool_name="read_file",
                status="ok",
                content="content",
                data={
                    "path": str((tmp_path.parent / "outside" / "secret.txt").resolve()),
                    "arguments": {
                        "filePath": str((tmp_path.parent / "outside" / "secret.txt").resolve())
                    },
                },
            ),
        ),
    )

    assert [context.path for context in contexts] == ["README.md"]
