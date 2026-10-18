"""Admission pins trusted source bytes; it never imports the candidate plugin."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

import jsonschema

from ..sdk.json_values import json_wire_object
from ..sdk.manifest import MANIFEST_FILE_NAME, PluginFileIdentity, parse_plugin_manifest
from ..sdk.plugin import HostPlugin
from .storage.store import canonical_json


def admit_plugin(
    source: Path,
    *,
    artifacts: Path,
    trusted: bool,
    config: Mapping[str, object],
    capabilities: frozenset[str],
) -> HostPlugin:
    if not trusted:
        raise PermissionError("plugin source requires explicit user trust before admission")
    source = source.resolve(strict=True)
    manifest = parse_plugin_manifest(json.loads((source / MANIFEST_FILE_NAME).read_text(encoding="utf-8")))
    if not manifest.files:
        raise ValueError(f"plugin {manifest.plugin_id} must declare implementation files")
    declared = {item.path for item in manifest.files}
    entry_module = manifest.entrypoint.partition(":")[0].replace(".", "/")
    if f"{entry_module}.py" not in declared and f"{entry_module}/__init__.py" not in declared:
        raise ValueError("plugin entrypoint is not in its declared implementation files")
    for path in source.rglob("*.py"):
        if path.relative_to(source).as_posix() not in declared:
            raise ValueError(f"plugin has undeclared Python source: {path.relative_to(source)}")
    requested = frozenset(item.name for item in manifest.capabilities)
    if not requested <= capabilities:
        raise PermissionError(f"plugin {manifest.plugin_id} requests ungranted capabilities")
    try:
        jsonschema.Draft202012Validator(json_wire_object(manifest.config_schema)).validate(json_wire_object(config))
    except jsonschema.ValidationError as exc:
        raise ValueError(f"plugin {manifest.plugin_id} configuration failed schema validation") from exc

    manifest_json = canonical_json(manifest.as_payload())
    identity = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    artifacts.mkdir(parents=True, exist_ok=True)
    destination = artifacts / identity
    if destination.exists():
        _verify_artifact(destination, manifest_json, manifest.files)
    else:
        with tempfile.TemporaryDirectory(prefix=".admit-", dir=artifacts) as temporary:
            candidate = Path(temporary) / "root"
            candidate.mkdir()
            for item in manifest.files:
                origin = (source / item.path).resolve(strict=True)
                if not origin.is_relative_to(source) or not origin.is_file():
                    raise ValueError("plugin file escapes its source root or is not a regular file")
                target = candidate / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                with origin.open("rb") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                with target.open("rb") as reader:
                    digest = hashlib.file_digest(reader, "sha256").hexdigest()
                if digest != item.sha256:
                    raise ValueError(f"plugin file identity changed: {item.path}")
            (candidate / MANIFEST_FILE_NAME).write_text(manifest_json, encoding="utf-8")
            try:
                os.rename(candidate, destination)
            except FileExistsError:
                _verify_artifact(destination, manifest_json, manifest.files)
    return HostPlugin(destination, manifest, config, requested)


def _verify_artifact(root: Path, manifest_json: str, identities: tuple[PluginFileIdentity, ...]) -> None:
    if (root / MANIFEST_FILE_NAME).read_text(encoding="utf-8") != manifest_json:
        raise ValueError("admitted plugin manifest was modified")
    for item in identities:
        path = (root / item.path).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("admitted plugin resource escaped its artifact root")
        with path.open("rb") as reader:
            digest = hashlib.file_digest(reader, "sha256").hexdigest()
        if digest != item.sha256:
            raise ValueError(f"admitted plugin artifact was modified: {item.path}")
