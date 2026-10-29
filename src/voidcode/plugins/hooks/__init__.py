"""Hooks plugin: user argv hooks for the four surfaces the core offers."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin

from .hooks_plugin import ToolHooks, parse_hooks_config


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    parsed = parse_hooks_config(config)
    hooks = ToolHooks(parsed)
    enabled = parsed.enabled is True
    return Plugin(
        pre_tool=hooks.pre_tool if enabled and parsed.pre_tool else None,
        pre_tool_match=tuple(parsed.pre_tool_match) if parsed.pre_tool_match else ("*",),
        pre_tool_mutating=True,
        post_tool=hooks.post_tool if enabled and parsed.post_tool else None,
        post_tool_match=tuple(parsed.post_tool_match) if parsed.post_tool_match else ("*",),
        post_tool_mutating=True,
        session_start=hooks.lifecycle if enabled and parsed.on_session_start else None,
        session_end=hooks.lifecycle if enabled and parsed.on_session_end else None,
    )
