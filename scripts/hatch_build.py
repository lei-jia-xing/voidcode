from __future__ import annotations

import os
import shutil
import subprocess
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
        self._build_sandbox_helper(root)

    def _build_sandbox_helper(self, root: Path) -> None:
        """Build the native sandbox helper into the wheel, or fail loudly for wheels.

        sdist builds skip the cargo stage (sources ship; the helper builds at install).
        Wheel builds require cargo: a wheel advertising confinement without its helper
        would be a lie, so a missing toolchain or build failure fails the wheel.
        Set VOIDCODE_SKIP_HELPER_BUILD=1 to force-skip (package stays importable; the
        sandbox plugin refuses at admission with the disable hint).
        """
        if os.environ.get("VOIDCODE_SKIP_HELPER_BUILD") == "1":
            return
        manifest = root / "native" / "sandbox-adapter" / "Cargo.toml"
        if not manifest.is_file():
            return
        target = self.target_name if hasattr(self, "target_name") else ""
        if target == "sdist":
            return
        cargo = shutil.which("cargo")
        if cargo is None:
            raise RuntimeError("sandbox helper build requires cargo (rustup.rs); set VOIDCODE_SKIP_HELPER_BUILD=1 to ship without the helper")
        target_dir = root / "native" / "sandbox-adapter" / "target"
        profile = "release" if os.environ.get("VOIDCODE_HELPER_DEBUG") != "1" else "debug"
        command = [cargo, "build", "--locked", "--manifest-path", str(manifest)]
        if profile == "release":
            command.append("--release")
        completed = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=1800)
        if completed.returncode != 0:
            raise RuntimeError(f"sandbox helper build failed:\n{completed.stdout}\n{completed.stderr}")
        exe = "voidcode-sandbox-helper.exe" if os.name == "nt" else "voidcode-sandbox-helper"
        built = target_dir / profile / exe
        if not built.is_file():
            raise RuntimeError(f"sandbox helper build produced no binary at {built}")
        staged_bin = root / "src" / "voidcode" / "bin"
        staged_bin.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, staged_bin / exe)
