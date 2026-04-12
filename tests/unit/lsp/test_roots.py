from __future__ import annotations

from pathlib import Path

from voidcode.lsp import discover_workspace_root


def test_discover_workspace_root_uses_nearest_marker_inside_workspace(tmp_path: Path) -> None:
    project_root = tmp_path / "apps" / "demo"
    source_file = project_root / "src" / "sample.py"
    project_root.mkdir(parents=True)
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("x = 1\n", encoding="utf-8")
    (project_root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    resolved = discover_workspace_root(
        file_path=source_file,
        workspace_root=tmp_path,
        root_markers=("pyproject.toml", ".git"),
    )

    assert resolved == project_root


def test_discover_workspace_root_falls_back_to_workspace_root_when_no_marker(
    tmp_path: Path,
) -> None:
    source_file = tmp_path / "src" / "sample.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("x = 1\n", encoding="utf-8")

    resolved = discover_workspace_root(
        file_path=source_file,
        workspace_root=tmp_path,
        root_markers=("pyproject.toml", ".git"),
    )

    assert resolved == tmp_path


def test_discover_workspace_root_ignores_paths_outside_workspace(tmp_path: Path) -> None:
    external_root = tmp_path.parent / "external-lsp-root"
    external_root.mkdir(exist_ok=True)
    external_file = external_root / "sample.py"
    external_file.write_text("x = 1\n", encoding="utf-8")

    resolved = discover_workspace_root(
        file_path=external_file,
        workspace_root=tmp_path,
        root_markers=("pyproject.toml", ".git"),
    )

    assert resolved == tmp_path
