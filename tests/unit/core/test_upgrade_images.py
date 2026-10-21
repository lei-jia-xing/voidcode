"""Behavior tests for P5 images: identity, materialization, bundle, and launch provenance.

Every assertion drives the real helpers over real files and one real child process: the digest must
name the bytes that execute, the staged web bundle must be part of that identity, one tree must not
be able to masquerade as another, and a pinned launch must not import a shadowing ``voidcode``
package from the working directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from voidcode.core.images import (
    BUNDLE_DIR_NAME,
    IMAGE_DIGEST_ENV,
    IMAGE_ROOT_ENV,
    PACKAGE_NAME,
    bundle_of,
    ensure_running_image,
    image_for_digest,
    installed_identity,
    launch_argv,
    launch_environment,
    materialize,
    package_digest,
    resolve_package_root,
    running_identity,
    running_package_root,
    validate_candidate,
)
from voidcode.core.transport.static import distribution_for

_PROBE = "import json, pathlib\nprint(json.dumps({'file': str(pathlib.Path(__file__).resolve())}))\n"


def _installation(root: Path, *, marker: str = "candidate", bundle_text: str = "index-v1") -> Path:
    """A minimal repo-layout installation: ``<root>/src/voidcode`` plus ``<root>/frontend/dist``."""
    package = root / "src" / PACKAGE_NAME
    (package / "core").mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""minimal installation {marker}"""\n', encoding="utf-8")
    (package / "core" / "__init__.py").write_text("", encoding="utf-8")
    (package / "probe.py").write_text(_PROBE, encoding="utf-8")
    (package / "data.json").write_text(json.dumps({"marker": marker}), encoding="utf-8")
    distribution = root / "frontend" / "dist"
    (distribution / "assets").mkdir(parents=True)
    (distribution / "index.html").write_text(bundle_text, encoding="utf-8")
    (distribution / "assets" / "app.js").write_text("export const version = 1;\n", encoding="utf-8")
    return root


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=60)


def test_package_digest_tracks_payload_bytes_and_ignores_caches(tmp_path: Path) -> None:
    source = _installation(tmp_path / "src-install")
    package = resolve_package_root(source)
    original = package_digest(package)

    # A faithful copy carries the source layout (package plus its served bundle) and hashes equal.
    copy_root = tmp_path / "copy-install"
    shutil.copytree(source, copy_root)
    copy = resolve_package_root(copy_root)
    assert package_digest(copy) == original

    cache = copy / "__pycache__"
    cache.mkdir()
    (cache / "probe.cpython-312.pyc").write_bytes(b"stale bytecode")
    assert package_digest(copy) == original, "bytecode caches must not change an image identity"

    (copy / "data.json").write_text(json.dumps({"marker": "changed"}), encoding="utf-8")
    mutated = package_digest(copy)
    assert mutated != original

    nested_root = tmp_path / "__pycache__" / "copy-install"
    nested_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, nested_root)
    nested = resolve_package_root(nested_root)
    assert package_digest(nested) == original, "a cache-named ancestor directory must not hide the tree's own files"

    # The packaged copy of the bundle is not served in the source layout, so a stale one is
    # ignored: the live tree and the image materialized from it hash the same served bytes.
    staged = copy / BUNDLE_DIR_NAME
    staged.mkdir()
    (staged / "index.html").write_text("stale packaged copy", encoding="utf-8")
    assert package_digest(copy) == mutated


def test_materialize_stages_the_bundle_into_a_content_addressed_image(tmp_path: Path) -> None:
    images = tmp_path / "images"
    first = materialize(_installation(tmp_path / "first"), images)

    assert first.root.name == "root" and first.root.parent.name == first.digest
    assert first.digest == package_digest(first.package_root)
    assert first.bundle == bundle_of(first.package_root) is not None
    staged = first.package_root / BUNDLE_DIR_NAME
    assert (staged / "index.html").read_text(encoding="utf-8") == "index-v1"
    assert (staged / "assets" / "app.js").is_file()
    assert distribution_for(first.package_root) == staged, "the transport must serve the image's own bundle"

    # A sibling directory next to the image is not part of the image's attested identity: a decoy
    # distribution must never override the bundle the digest covers.
    decoy = first.root.parent / "frontend" / "dist"
    decoy.mkdir(parents=True)
    (decoy / "index.html").write_text("decoy", encoding="utf-8")
    assert distribution_for(first.package_root) == staged
    assert (staged / "index.html").read_text(encoding="utf-8") == "index-v1"
    assert image_for_digest(images, first.digest).digest == first.digest

    second = materialize(_installation(tmp_path / "second", bundle_text="index-v2"), images)
    assert second.digest != first.digest, "different served assets are a different image"


def test_materialize_reuses_an_existing_image_and_verifies_stored_bytes(tmp_path: Path) -> None:
    images = tmp_path / "images"
    installation = _installation(tmp_path / "install")
    first = materialize(installation, images)
    again = materialize(installation, images)

    assert again.digest == first.digest
    assert list(images.glob(".materialize-*")) == [], "no scratch directory may outlive materialization"
    assert (first.package_root / "data.json").read_bytes() == (again.package_root / "data.json").read_bytes()

    (first.package_root / "data.json").write_text(json.dumps({"marker": "tampered"}), encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to run it"):
        image_for_digest(images, first.digest)
    with pytest.raises(ValueError, match="refusing to run it"):
        materialize(installation, images)
    with pytest.raises(FileNotFoundError):
        image_for_digest(images, "0" * 64)


def test_resolve_package_root_accepts_each_layout_and_refuses_non_installations(tmp_path: Path) -> None:
    installation = _installation(tmp_path / "install")
    package = installation / "src" / PACKAGE_NAME
    assert resolve_package_root(installation) == package
    assert resolve_package_root(installation / "src") == package
    assert resolve_package_root(package) == package

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a VoidCode installation"):
        resolve_package_root(plain)


def test_validate_candidate_refuses_the_active_runtime_and_workspace_copies(tmp_path: Path) -> None:
    installation = _installation(tmp_path / "candidate")
    package = installation / "src" / PACKAGE_NAME
    active = running_package_root()

    assert validate_candidate(installation, active_package_root=active) == package
    with pytest.raises(ValueError, match="active runtime"):
        validate_candidate(active, active_package_root=active)
    with pytest.raises(ValueError, match="must not live inside the workspace"):
        validate_candidate(installation, active_package_root=active, workspace=tmp_path)


def test_materialize_refuses_a_source_that_moves_mid_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import voidcode.core.images as images_module

    installation = _installation(tmp_path / "moving")
    real_copy_tree = images_module._copy_tree

    def moving_copy(source: Path, destination: Path) -> None:
        (source / "data.json").write_text("mutated mid-copy", encoding="utf-8")
        real_copy_tree(source, destination)

    monkeypatch.setattr(images_module, "_copy_tree", moving_copy)
    with pytest.raises(ValueError, match="changed during materialization"):
        materialize(installation, tmp_path / "images")
    assert list((tmp_path / "images").glob(".materialize-*")) == []
    assert list((tmp_path / "images").iterdir()) == []


def test_ensure_running_image_snapshots_an_unsupervised_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMAGE_ROOT_ENV, raising=False)
    monkeypatch.delenv(IMAGE_DIGEST_ENV, raising=False)
    images = tmp_path / "images"
    # The snapshot is the running tree *plus* the bundle it serves, so an explicit materialization
    # of the same tree is the reference identity.
    direct = materialize(running_package_root(), images)
    snapshot = ensure_running_image(images)

    assert snapshot.digest == direct.digest
    assert snapshot.package_root.is_relative_to(images)
    assert ensure_running_image(images).digest == snapshot.digest

    monkeypatch.setenv(IMAGE_ROOT_ENV, str(snapshot.root))
    monkeypatch.setenv(IMAGE_DIGEST_ENV, snapshot.digest)
    monkeypatch.setattr("voidcode.core.images.running_package_root", lambda: snapshot.package_root)
    unused = tmp_path / "unused"
    assert ensure_running_image(unused).digest == snapshot.digest, "a pinned runtime is reused, not copied again"
    assert not unused.exists(), "reusing a pinned runtime must not materialize anything"


def test_pinned_launch_executes_the_image_and_never_a_shadow_package(tmp_path: Path) -> None:
    image = materialize(_installation(tmp_path / "install"), tmp_path / "images")
    workspace = tmp_path / "workspace"
    (workspace / PACKAGE_NAME).mkdir(parents=True)
    marker = workspace / "SHADOW_EXECUTED"
    (workspace / PACKAGE_NAME / "__init__.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('shadow')\n",
        encoding="utf-8",
    )

    assert launch_argv("serve") == [sys.executable, "-P", "-m", PACKAGE_NAME, "serve"]
    pinned = _run([sys.executable, "-P", "-m", f"{PACKAGE_NAME}.probe"], cwd=workspace, env=launch_environment(image))
    assert pinned.returncode == 0, pinned.stderr
    executed = Path(json.loads(pinned.stdout)["file"])
    assert executed.is_relative_to(image.package_root), executed
    assert not marker.exists(), "a pinned launch must never import a package from the working directory"

    # Control: without `-P` the same environment resolves the shadowing package first, which is what
    # the pinned launch exists to prevent.
    unpinned = _run(
        [sys.executable, "-m", f"{PACKAGE_NAME}.probe"],
        cwd=workspace,
        env={**launch_environment(image), "PYTHONPATH": str(image.root)},
    )
    assert marker.exists(), "the harmless control must demonstrate the shadowing import the pin prevents"
    assert unpinned.returncode != 0


def test_running_identity_verifies_the_pinned_root_and_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(IMAGE_ROOT_ENV, raising=False)
    monkeypatch.delenv(IMAGE_DIGEST_ENV, raising=False)
    assert running_identity() is None

    package_root = running_package_root()
    digest = package_digest(package_root)
    assert installed_identity().digest == digest

    monkeypatch.setenv(IMAGE_ROOT_ENV, str(package_root.parent))
    monkeypatch.setenv(IMAGE_DIGEST_ENV, digest)
    pinned = running_identity()
    assert pinned is not None and pinned.digest == digest

    monkeypatch.setenv(IMAGE_DIGEST_ENV, "deadbeef")
    with pytest.raises(ValueError, match="does not match the executing tree"):
        running_identity()

    monkeypatch.setenv(IMAGE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(IMAGE_DIGEST_ENV, digest)
    with pytest.raises(ValueError, match="outside its pinned image"):
        running_identity()
