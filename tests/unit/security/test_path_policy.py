from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.security.path_policy import WorkspacePathResolution, resolve_workspace_path


@pytest.mark.parametrize(
    "raw_path",
    [
        "../outside.txt",
        "../../escape",
        "a/../../outside.txt",
        "/etc/passwd",
        "/tmp/absolute-outside",
    ],
)
def test_resolve_workspace_path_rejects_paths_outside_workspace(
    tmp_path: Path,
    raw_path: str,
) -> None:
    with pytest.raises(ValueError, match="path must be inside the workspace"):
        resolve_workspace_path(workspace=tmp_path, raw_path=raw_path)


def test_resolve_workspace_path_allow_outside_workspace_marks_external(tmp_path: Path) -> None:
    outside = tmp_path / ".." / "outside-target"
    resolution = resolve_workspace_path(
        workspace=tmp_path,
        raw_path=str(outside),
        allow_outside_workspace=True,
    )
    assert resolution.is_external is True
    assert not resolution.candidate.is_relative_to(tmp_path.resolve())
    # External paths are reported as absolute posix paths, not relative.
    assert resolution.relative_path.startswith("/")


def test_resolve_workspace_path_normalizes_dot_segments(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("data", encoding="utf-8")
    resolution = resolve_workspace_path(workspace=tmp_path, raw_path="a/../real.txt")
    assert resolution.candidate == (tmp_path / "real.txt").resolve()
    assert resolution.relative_path == "real.txt"
    assert resolution.is_external is False


def test_resolve_workspace_path_resolves_absolute_path_inside_workspace(tmp_path: Path) -> None:
    target = tmp_path / "inside.txt"
    target.write_text("data", encoding="utf-8")
    resolution = resolve_workspace_path(workspace=tmp_path, raw_path=str(target))
    assert resolution.candidate == target.resolve()
    assert resolution.relative_path == "inside.txt"
    assert resolution.is_external is False


def test_resolve_workspace_path_rejects_containment_for_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "symlink-escape-target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unsupported on this filesystem")
    with pytest.raises(ValueError, match="path must be inside the workspace"):
        resolve_workspace_path(workspace=tmp_path, raw_path="link")


def test_resolve_workspace_path_require_existing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="target does not exist"):
        resolve_workspace_path(workspace=tmp_path, raw_path="missing.txt", require_existing=True)

    (tmp_path / "present.txt").write_text("data", encoding="utf-8")
    resolution = resolve_workspace_path(workspace=tmp_path, raw_path="present.txt", require_existing=True)
    assert resolution.relative_path == "present.txt"


def test_resolve_workspace_path_custom_containment_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="custom containment"):
        resolve_workspace_path(
            workspace=tmp_path,
            raw_path="/etc/hostname",
            containment_error="custom containment message",
        )


def test_resolve_workspace_path_require_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "subdir"
    directory.mkdir()
    with pytest.raises(ValueError, match="target is not a regular file"):
        resolve_workspace_path(
            workspace=tmp_path,
            raw_path="subdir",
            require_regular_file=True,
            regular_file_error="target is not a regular file",
        )

    (tmp_path / "file.txt").write_text("data", encoding="utf-8")
    resolution = resolve_workspace_path(
        workspace=tmp_path,
        raw_path="file.txt",
        require_regular_file=True,
    )
    assert resolution.relative_path == "file.txt"


def test_resolve_workspace_path_expands_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home_dir = tmp_path.parent / "expanded-home"
    home_dir.mkdir(exist_ok=True)
    (home_dir / "profile.txt").write_text("data", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home_dir))
    resolution = resolve_workspace_path(
        workspace=tmp_path,
        raw_path="~/profile.txt",
        allow_outside_workspace=True,
    )
    assert resolution.candidate == (home_dir / "profile.txt").resolve()
    assert resolution.is_external is True


def test_resolve_workspace_path_defaults_to_workspace_relative(tmp_path: Path) -> None:
    (tmp_path / "leaf.txt").write_text("data", encoding="utf-8")
    resolution = resolve_workspace_path(workspace=tmp_path, raw_path="leaf.txt")
    assert isinstance(resolution, WorkspacePathResolution)
    assert resolution.workspace_root == tmp_path.resolve()
    assert resolution.relative_path == "leaf.txt"
    assert resolution.is_external is False
