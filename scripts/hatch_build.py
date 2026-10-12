from __future__ import annotations

import shutil
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        _ = version, build_data
        root = Path(self.root)
        frontend_dist = root / "frontend" / "dist"
        staged_dist = root / "src" / "voidcode" / "_web_dist"

        if frontend_dist.is_dir() and (frontend_dist / "index.html").is_file():
            if staged_dist.exists():
                shutil.rmtree(staged_dist)
            shutil.copytree(frontend_dist, staged_dist)
