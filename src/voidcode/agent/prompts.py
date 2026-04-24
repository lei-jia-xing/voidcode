from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent
_PROMPT_FILE_NAME = "base.txt"
_BUILTIN_PROMPT_PROFILES = frozenset(
    {"leader", "worker", "advisor", "explore", "researcher", "product"}
)


def _prompt_path(prompt_profile: str) -> Path:
    return _AGENT_DIR / prompt_profile / _PROMPT_FILE_NAME


def is_builtin_prompt_profile(prompt_profile: str) -> bool:
    normalized_prompt_profile = prompt_profile.strip()
    return normalized_prompt_profile in _BUILTIN_PROMPT_PROFILES


def has_builtin_prompt_profile(prompt_profile: str) -> bool:
    normalized_prompt_profile = prompt_profile.strip()
    if not is_builtin_prompt_profile(normalized_prompt_profile):
        return False
    return _prompt_path(normalized_prompt_profile).is_file()


@cache
def _render_known_builtin_prompt_profile(prompt_profile: str) -> str | None:
    path = _prompt_path(prompt_profile)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def render_builtin_prompt_profile(prompt_profile: str) -> str | None:
    normalized_prompt_profile = prompt_profile.strip()
    if not is_builtin_prompt_profile(normalized_prompt_profile):
        return None
    return _render_known_builtin_prompt_profile(normalized_prompt_profile)


def render_agent_prompt(agent_preset: Mapping[str, object] | None = None) -> str | None:
    if agent_preset is None:
        return None
    prompt_profile = agent_preset.get("prompt_profile")
    if not isinstance(prompt_profile, str) or not prompt_profile.strip():
        return None
    normalized_prompt_profile = prompt_profile.strip()
    builtin_prompt = render_builtin_prompt_profile(normalized_prompt_profile)
    if builtin_prompt is not None:
        return builtin_prompt
    return (
        "Runtime-selected VoidCode agent prompt profile: "
        f"{normalized_prompt_profile}. Treat this as the active agent role profile "
        "for this single-agent turn while still following the runtime-provided tool "
        "and skill boundaries."
    )


__all__ = [
    "has_builtin_prompt_profile",
    "is_builtin_prompt_profile",
    "render_agent_prompt",
    "render_builtin_prompt_profile",
]
