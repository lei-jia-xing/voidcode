"""Pin bundled plugin source identities without importing plugin code.

Run after editing/formatting plugin sources:
    uv run python scripts/generate_plugin_identities.py
    uv run python scripts/generate_plugin_identities.py --check
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from voidcode.sdk.manifest import parse_plugin_manifest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "src" / "voidcode" / "plugins"


def main(argv: list[str]) -> int:
    stale = False
    manifests = tuple(sorted(ROOT.glob("*/voidcode-plugin.json")))
    if not manifests:
        raise ValueError("no bundled plugin manifests found")
    for path in manifests:
        original = path.read_text(encoding="utf-8")
        payload = json.loads(original)
        manifest = parse_plugin_manifest(payload)
        names = {source.relative_to(path.parent).as_posix() for source in path.parent.rglob("*.py")}
        names.update(identity.path for identity in manifest.files if not identity.path.endswith(".py"))
        identities = []
        for name in sorted(names):
            source = (path.parent / name).resolve(strict=True)
            if not source.is_relative_to(path.parent.resolve()) or not source.is_file():
                raise ValueError(f"plugin resource escapes its source root: {name}")
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            identities.append({"path": name, "sha256": digest})
        payload["files"] = identities
        expected = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if original != expected:
            if "--check" in argv:
                print(f"{path} is stale: run uv run python scripts/generate_plugin_identities.py", file=sys.stderr)
                stale = True
            else:
                path.write_text(expected, encoding="utf-8")
                print(f"wrote {path}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
