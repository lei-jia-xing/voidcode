from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
else:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        _ = version
        root = Path(self.root)
        frontend_dist = root / "frontend" / "dist"
        staged_dist = root / "src" / "voidcode" / "_web_dist"

        if frontend_dist.is_dir() and (frontend_dist / "index.html").is_file():
            if staged_dist.exists():
                shutil.rmtree(staged_dist)
            shutil.copytree(frontend_dist, staged_dist)

        if not staged_dist.is_dir() or not (staged_dist / "index.html").is_file():
            return

        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise TypeError("build_data.force_include must be a mapping")
        force_include_map = cast(dict[str, str], force_include)
        target_path = (
            "src/voidcode/_web_dist"
            if getattr(self, "target_name", "") == "sdist"
            else "voidcode/_web_dist"
        )
        force_include_map[str(staged_dist)] = target_path
