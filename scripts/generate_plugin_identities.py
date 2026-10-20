"""Pin plugin source identities without importing plugin code.

Bundled sweep (default, unchanged):
    uv run python scripts/generate_plugin_identities.py
    uv run python scripts/generate_plugin_identities.py --check

One plugin directory at a time (external author, or one bundled plugin):
    uv run python scripts/generate_plugin_identities.py --plugin-dir path/to/plugin [--check]

Drop a deleted non-Python resource from one pinned manifest (never hand-edit files):
    uv run python scripts/generate_plugin_identities.py --plugin-dir path/to/plugin --drop-resource old-notes.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voidcode.sdk.manifest import parse_plugin_manifest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "src" / "voidcode" / "plugins"


def _manifest_targets(plugin_dirs: list[str]) -> tuple[Path, ...]:
    if not plugin_dirs:
        manifests = tuple(sorted(ROOT.glob("*/voidcode-plugin.json")))
        if not manifests:
            raise ValueError("no bundled plugin manifests found")
        return manifests
    targets: list[Path] = []
    for raw in plugin_dirs:
        path = Path(raw) / "voidcode-plugin.json"
        if not path.is_file():
            raise ValueError(f"no plugin manifest in --plugin-dir {raw}")
        targets.append(path)
    return tuple(targets)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Pin plugin source identities without importing plugin code.")
    parser.add_argument("--check", action="store_true", help="report stale manifests without writing anything")
    parser.add_argument(
        "--plugin-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="pin one plugin directory (repeatable); default sweeps the bundled plugins",
    )
    parser.add_argument(
        "--drop-resource",
        action="append",
        default=[],
        metavar="PATH",
        help="forget one declared non-Python resource (repeatable); requires exactly one --plugin-dir",
    )
    args = parser.parse_args(argv)
    if args.drop_resource and len(args.plugin_dir) != 1:
        parser.error("--drop-resource requires exactly one --plugin-dir")
    drops = set(args.drop_resource)
    rerun = "uv run python scripts/generate_plugin_identities.py" + "".join(f" --plugin-dir {shlex.quote(item)}" for item in args.plugin_dir)
    rerun += "".join(f" --drop-resource {shlex.quote(item)}" for item in args.drop_resource)
    stale = False
    for path in _manifest_targets(args.plugin_dir):
        original = path.read_text(encoding="utf-8")
        payload = json.loads(original)
        manifest = parse_plugin_manifest(payload)
        names = {source.relative_to(path.parent).as_posix() for source in path.parent.rglob("*.py")}
        resources = {identity.path for identity in manifest.files if not identity.path.endswith(".py")}
        unknown = sorted(drops - resources)
        if unknown:
            raise ValueError(f"not declared non-Python resource(s): {', '.join(unknown)}")
        names.update(resources - drops)
        identities = []
        for name in sorted(names):
            source = (path.parent / name).resolve(strict=True)
            if not source.is_relative_to(path.parent.resolve()) or not source.is_file():
                raise ValueError(f"plugin resource escapes its source root: {name}")
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            identities.append({"path": name, "sha256": digest})
        payload["files"] = identities
        parse_plugin_manifest(payload)  # validate the final pinned manifest before writing it
        expected = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if original != expected:
            if args.check:
                print(f"{path} is stale: run {rerun}", file=sys.stderr)
                stale = True
            else:
                path.write_text(expected, encoding="utf-8")
                print(f"wrote {path}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
