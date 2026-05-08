from __future__ import annotations

import shutil
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    source = repo_root / "frontend" / "dist"
    target = repo_root / "src" / "voidcode" / "_web_dist"

    if not source.is_dir() or not (source / "index.html").is_file():
        raise SystemExit(
            "frontend build output is missing; run `bun run build` in frontend/ before staging"
        )

    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
