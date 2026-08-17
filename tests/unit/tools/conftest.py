from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

_LANG_PACK_CACHE_DIRNAME = "tree-sitter-language-pack"


@pytest.fixture(scope="session")
def _tree_sitter_language_pack_cache_mirror(
    tmp_path_factory: pytest.TempPathFactory,
) -> Path | None:
    """Copy the machine's tree-sitter-language-pack cache into a session mirror.

    ``tree-sitter-language-pack`` keeps downloaded grammar shared libraries in
    a cache directory under ``XDG_CACHE_HOME`` and downloads any missing
    grammar on demand. Tests below exercise post-edit syntax validation, so
    the per-test XDG isolation (``tests/conftest.py``) would otherwise force a
    network download on every parse — hanging for minutes when offline.

    When the developer machine already has a populated cache (e.g. from normal
    tool use at ``~/.cache/tree-sitter-language-pack``), copy it into a
    session-scoped mirror once so per-test seeding below is cheap and fully
    isolated from the real cache. Returns ``None`` when no local cache exists;
    tests then fall back to the pack's normal network behavior.
    """
    real_cache = Path.home() / ".cache" / _LANG_PACK_CACHE_DIRNAME
    if not real_cache.is_dir():
        return None
    mirror = tmp_path_factory.mktemp(_LANG_PACK_CACHE_DIRNAME)
    for version_dir in real_cache.iterdir():
        if version_dir.is_dir():
            shutil.copytree(version_dir, mirror / version_dir.name)
    return mirror


@pytest.fixture(autouse=True)
def _seed_tree_sitter_language_pack_cache(
    _isolated_xdg_runtime_dirs: None,
    _tree_sitter_language_pack_cache_mirror: Path | None,
) -> None:
    """Expose the language-pack cache at the per-test isolated XDG cache path.

    Depends on the parent ``tests/conftest.py`` XDG isolation fixture, so it
    always runs after ``XDG_CACHE_HOME`` has been redirected to the per-test
    directory. A directory symlink makes seeding O(1) per test while keeping
    the data in the session-scoped mirror.
    """
    if _tree_sitter_language_pack_cache_mirror is None:
        return
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    link_path = cache_home / _LANG_PACK_CACHE_DIRNAME
    if link_path.is_symlink():
        return
    if link_path.exists():
        # The pack scaffolds its cache dir (e.g. a failed download left
        # ``.download.lock``) before seeding could run; it holds no real data.
        shutil.rmtree(link_path)
    try:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(_tree_sitter_language_pack_cache_mirror, target_is_directory=True)
    except OSError:
        # Symlinks unsupported (e.g. restricted CI filesystems): leave the
        # pack on its default (network) behavior instead of failing tests.
        return
