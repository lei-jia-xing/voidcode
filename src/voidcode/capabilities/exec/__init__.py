"""Shared workspace execution capabilities and optional plugin factory.

Owns the shell execution tool: process-group lifetime,
bounded capture and the honest partial/settled reporting the runtime relies on.
It also owns background process management on the core's process lease:
supervised processes the host owns, whose identity this plugin persists so a
later host can report them honestly instead of guessing.
"""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .background_process import BackgroundProcessTool, prepare_exec_deactivation
from .shell_exec import ShellExecTool


def create_plugin(_config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(tools=(ShellExecTool(), BackgroundProcessTool()), prepare_deactivate=prepare_exec_deactivation)
