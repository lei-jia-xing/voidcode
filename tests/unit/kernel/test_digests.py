"""Behavior tests for source-identity digests.

A digest is a version pin, so two different trees must never share one. These cases are the framing
regressions (a file whose content looks like another file's path and bytes), the cache rule, and both
selections the kernel uses: an installation tree and a Python source tree.
"""

from __future__ import annotations

from pathlib import Path

from voidcode.kernel.digests import package_files, package_tree_digest, python_tree_digest


def _write(root: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_package_digest_frames_each_file(tmp_path: Path) -> None:
    split = _write(tmp_path / "split", {"a": b"1", "b": b"2"})
    fused = _write(tmp_path / "fused", {"a": b"1b\x002"})
    assert package_tree_digest(split) != package_tree_digest(fused)


def test_python_digest_frames_each_module(tmp_path: Path) -> None:
    """The same byte stream under a different module layout must not share an SDK pin."""
    split = _write(tmp_path / "split-pkg", {"__init__.py": b"", "a.py": b"#", "bc.py": b"pass\n"})
    fused = _write(tmp_path / "fused-pkg", {"__init__.py": b"", "a.py": b"#b", "c.py": b"pass\n"})
    assert python_tree_digest(split) != python_tree_digest(fused)


def test_identity_ignores_caches_relative_to_the_hashed_root(tmp_path: Path) -> None:
    install = _write(tmp_path / "__pycache__" / "install", {"__init__.py": b"", "impl.py": b"value = 1\n"})
    plain = _write(tmp_path / "plain", {"__init__.py": b"", "impl.py": b"value = 1\n"})
    assert len(package_files(install)) == 2, "a tree living under a cache-named directory still hashes its own files"
    assert package_tree_digest(install) == package_tree_digest(plain)

    _write(install, {"__pycache__/impl.cpython-312.pyc": b"stale bytecode"})
    assert package_tree_digest(install) == package_tree_digest(plain), "caches inside the tree are ignored"
    assert python_tree_digest(install) == python_tree_digest(plain)
