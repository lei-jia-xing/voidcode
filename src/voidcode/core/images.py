"""Content-addressed core images: the immutable bytes a supervised child executes.

An image is a copy of one installation's package tree (plus the web bundle it serves) under
``<state_dir>/kernel-images/<digest>/root``. The supervisor launches children only from an image and
resolves the elected image from the digest its state pins, so a running installation is never
overwritten and "which code is executing" is answered by a hash instead of a directory that may have
changed since it was checked.

Nothing in this module imports core, store, or SDK internals: it is the part of P5 that can be
implemented and tested independently of the capability generation.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .digests import framed_files_digest, package_files
from .transport.static import distribution_for

#: Directory name under the state dir that holds every materialized image.
IMAGES_DIR_NAME = "kernel-images"
#: The launch root inside one image directory: what ``PYTHONPATH`` points at.
IMAGE_ROOT_DIR_NAME = "root"
#: The importable package directory inside an installation and inside an image root.
PACKAGE_NAME = "voidcode"
#: Where the served web client lives inside the package (both wheel and image layouts).
BUNDLE_DIR_NAME = "_web_dist"
#: Environment the supervisor uses to pin which image a child is allowed to execute.
IMAGE_ROOT_ENV = "VOIDCODE_IMAGE_ROOT"
IMAGE_DIGEST_ENV = "VOIDCODE_IMAGE_DIGEST"


@dataclass(frozen=True, slots=True)
class Image:
    """One immutable installation snapshot: its identity, its launch root, and its bundle."""

    digest: str
    root: Path
    package_root: Path
    bundle: str | None
    source: Path


def pin(image: Image, *, core_version: str, launch_protocol: int) -> dict[str, object]:
    """The image pin a revision records: the digest plus the build metadata it fixes.

    The digest covers the code and the served bundle; the version and protocol are the values this
    build runs with. The single authoritative shape: callers must not invent their own pin fields.
    """
    return {"digest": image.digest, "core_version": core_version, "launch_protocol": launch_protocol}


def package_digest(package_root: Path) -> str:
    """Identity of one installation tree: the framed digest over what it runs and serves.

    Asset bytes count (a staged web bundle is part of the identity, not a detail the digest could
    miss while the child serves different bytes than the one that was checked). A source checkout
    serves its freshly built bundle from outside the package, so the digest frames those served
    bytes under the staged name instead of the stale packaged copy: the live tree and the image
    materialized from it hash the same bytes.
    """
    root = Path(package_root).resolve(strict=True)
    distribution = distribution_for(root)
    stored = root / BUNDLE_DIR_NAME
    entries = [(path.relative_to(root).as_posix(), path) for path in package_files(root) if BUNDLE_DIR_NAME not in path.relative_to(root).parts]
    if distribution is not None:
        served = Path(distribution).resolve(strict=True)
        if served == stored.resolve():
            entries.extend(
                (path.relative_to(root).as_posix(), path) for path in package_files(root) if BUNDLE_DIR_NAME in path.relative_to(root).parts
            )
        else:
            entries.extend((f"{BUNDLE_DIR_NAME}/{path.relative_to(served).as_posix()}", path) for path in package_files(served))
    return framed_files_digest(entries)


def bundle_of(package_root: Path) -> str | None:
    """Digest of the web bundle one installation serves, or ``None`` when it serves no client."""
    distribution = distribution_for(Path(package_root).resolve(strict=True))
    if distribution is None:
        return None
    served = Path(distribution).resolve(strict=True)
    return framed_files_digest([(path.relative_to(served).as_posix(), path) for path in package_files(served)])


def resolve_package_root(candidate: Path) -> Path:
    """The package directory of a candidate installation: a repo checkout, ``src/voidcode``, or the package itself."""
    candidate = Path(candidate).resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError(f"candidate installation is not a directory: {candidate}")
    for relative in (Path(PACKAGE_NAME), Path("src") / PACKAGE_NAME, Path()):
        root = (candidate / relative).resolve()
        if root.name == PACKAGE_NAME and (root / "__init__.py").is_file():
            return root
    raise ValueError(f"candidate is not a VoidCode installation (no {PACKAGE_NAME}/__init__.py): {candidate}")


def validate_candidate(candidate: Path, *, active_package_root: Path, workspace: Path | None = None) -> Path:
    """Ingress rule for an upgrade request: return the candidate's package root or refuse.

    The active runtime's own tree is never a valid candidate (an upgrade replaces the running
    installation with *different* bytes), and a candidate under the workspace would be editable by
    the file tools after it was checked.
    """
    package_root = resolve_package_root(candidate)
    if package_root == Path(active_package_root).resolve(strict=True):
        raise ValueError("candidate resolves to the active runtime; an upgrade must name a different installation")
    if workspace is not None and package_root.is_relative_to(Path(workspace).resolve()):
        raise ValueError("candidate installation must not live inside the workspace")
    return package_root


def running_package_root() -> Path:
    """The package tree this process actually executes."""
    #: ``src/voidcode/core/images.py`` -> that process's ``voidcode`` package.
    return Path(__file__).resolve().parents[1]


def running_identity() -> Image | None:
    """Identity of THIS process: the pinned image when a supervisor launched it, else None.

    A pinned child verifies before answering: its executing package must live inside the pinned root
    and hash to the pinned digest, so a shadowed import path cannot pass for the elected image.
    """
    root = os.environ.get(IMAGE_ROOT_ENV)
    digest = os.environ.get(IMAGE_DIGEST_ENV)
    if root is None and digest is None:
        return None
    if not root or not digest:
        raise ValueError(f"{IMAGE_ROOT_ENV} and {IMAGE_DIGEST_ENV} must be set together")
    image_root = Path(root).resolve()
    package_root = running_package_root()
    if not package_root.is_relative_to(image_root):
        raise ValueError(f"process runs from {package_root} outside its pinned image {image_root}")
    actual = package_digest(package_root)
    if actual != digest:
        raise ValueError(f"pinned image digest {digest} does not match the executing tree {actual}")
    return Image(digest=actual, root=image_root, package_root=package_root, bundle=bundle_of(package_root), source=package_root)


def installed_identity() -> Image:
    """Identity of an unsupervised process (plain CLI use): digest of the tree it is executing."""
    package_root = running_package_root()
    return Image(
        digest=package_digest(package_root),
        root=package_root.parent,
        package_root=package_root,
        bundle=bundle_of(package_root),
        source=package_root,
    )


def ensure_running_image(images_dir: Path) -> Image:
    """The image for THIS process: the pinned one when a supervisor launched it, else a fresh snapshot.

    A supervised process is already running from an image, so its snapshot exists and nothing is
    copied; an ordinary installation is snapshotted once, because recovery has to relaunch the exact
    bytes that were running rather than whatever the directory holds later.
    """
    pinned = running_identity()
    return pinned if pinned is not None else materialize(running_package_root(), images_dir)


def materialize(candidate: Path, images_dir: Path) -> Image:
    """Copy one installation into a content-addressed image and return its identity.

    The bundle is staged before digesting, so the image serves the same assets its digest covers.
    Re-materializing an existing digest re-verifies the stored bytes instead of trusting the source.
    """
    source_root = resolve_package_root(candidate)
    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".materialize-", dir=images_dir))
    try:
        target_package = scratch / IMAGE_ROOT_DIR_NAME / PACKAGE_NAME
        before = package_digest(source_root)
        _copy_tree(source_root, target_package)
        if package_digest(source_root) != before:
            raise ValueError(
                f"candidate installation {source_root} changed during materialization; keep the candidate immutable between preflight and launch"
            )
        _stage_bundle(source_root, target_package)
        digest = package_digest(target_package)
        destination = images_dir / digest
        if destination.exists():
            return _verify_image(destination / IMAGE_ROOT_DIR_NAME, source=source_root)
        os.rename(scratch, destination)
        scratch = None
        return _verify_image(destination / IMAGE_ROOT_DIR_NAME, source=source_root)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def image_for_digest(images_dir: Path, digest: str) -> Image:
    """Resolve one elected image from its digest, verifying the stored bytes before returning."""
    image_root = Path(images_dir) / digest / IMAGE_ROOT_DIR_NAME
    if not (image_root / PACKAGE_NAME / "__init__.py").is_file():
        raise FileNotFoundError(f"elected image {digest} is not materialized under {images_dir}")
    return _verify_image(image_root, source=image_root)


def launch_environment_for_root(root: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment that imports exactly one installation tree: nothing from cwd or the workspace.

    ``PYTHONPATH`` is replaced rather than extended so a workspace or cwd package cannot shadow
    the authorized tree, and ``PYTHONHOME`` is dropped so it cannot redirect the standard library.
    The caller's working directory is preserved: it is never on the import path.
    """
    environment = dict(os.environ if base is None else base)
    environment["PYTHONPATH"] = str(root)
    environment.pop("PYTHONHOME", None)
    return environment


def launch_environment(image: Image, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for one pinned child: the image root is the only import root, and it is announced.

    The root/digest pair lets the child prove which image it is executing.
    """
    environment = launch_environment_for_root(image.root, base)
    environment[IMAGE_ROOT_ENV] = str(image.root)
    environment[IMAGE_DIGEST_ENV] = image.digest
    return environment


def launch_argv(*arguments: str) -> list[str]:
    """Command line for one pinned child: safe path mode, root facade, no cwd on the import path."""
    return [sys.executable, "-P", "-m", PACKAGE_NAME, *arguments]


def _copy_tree(source: Path, destination: Path) -> None:
    for path in package_files(source):
        target = destination / path.relative_to(source.resolve(strict=True))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def _stage_bundle(source_root: Path, target_package: Path) -> None:
    distribution = distribution_for(source_root)
    if distribution is None:
        return
    destination = target_package / BUNDLE_DIR_NAME
    if distribution.resolve() == destination.resolve():
        return
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(distribution, destination)


def _verify_image(image_root: Path, *, source: Path) -> Image:
    package_root = image_root / PACKAGE_NAME
    digest = package_digest(package_root)
    if image_root.parent.name != digest:
        raise ValueError(f"image at {image_root} hashes to {digest} instead of its own name; refusing to run it")
    return Image(digest=digest, root=image_root, package_root=package_root, bundle=bundle_of(package_root), source=source)
