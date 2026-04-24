from __future__ import annotations

from collections.abc import Mapping

from ..prompts import render_builtin_prompt_profile


def render_leader_prompt(agent_preset: Mapping[str, object] | None = None) -> str:
    _ = agent_preset
    prompt = render_builtin_prompt_profile("leader")
    if prompt is None:
        raise ValueError("builtin prompt profile 'leader' is not available")
    return prompt


__all__ = ["render_leader_prompt"]
