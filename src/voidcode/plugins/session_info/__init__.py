"""Session information plugin: read-only status/context/usage/export/copy panels.

The panels read the calling session's committed events and measured usage
through the SDK services; they never mutate state and never touch storage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, command_definition

from .panels import SessionInfoCommand


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    manifest = parse_plugin_manifest(json.loads((Path(__file__).parent / "voidcode-plugin.json").read_text(encoding="utf-8")))
    commands = tuple(SessionInfoCommand(command_definition(decl), decl.name) for decl in manifest.commands)
    return Plugin(commands=commands)
