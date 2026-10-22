from __future__ import annotations

from functools import cache
from importlib.resources import files
from importlib.resources.abc import Traversable

from .models import AgentPromptMaterialization

# Copied from ``voidcode.agent.prompt_sections``; the rendered text is identical.
_USER_APPEND_HEADING = "User-append: user-authored text is authoritative over generated blocks."
BUILTIN_PROMPT_PROFILES = ("default", "execute", "review", "explore", "research", "plan")
_BUILTIN_PROMPT_PROFILES = frozenset(BUILTIN_PROMPT_PROFILES)


def _prompt_resource(prompt_profile: str) -> Traversable:
    return files(__package__).joinpath("texts", f"{prompt_profile}.txt")


def is_builtin_prompt_profile(prompt_profile: str) -> bool:
    normalized_prompt_profile = prompt_profile.strip()
    return normalized_prompt_profile in _BUILTIN_PROMPT_PROFILES


def has_builtin_prompt_profile(prompt_profile: str) -> bool:
    normalized_prompt_profile = prompt_profile.strip()
    if not is_builtin_prompt_profile(normalized_prompt_profile):
        return False
    return _prompt_resource(normalized_prompt_profile).is_file()


@cache
def _render_known_builtin_prompt_profile(prompt_profile: str) -> str | None:
    resource = _prompt_resource(prompt_profile)
    if not resource.is_file():
        return None
    return resource.read_text(encoding="utf-8").strip()


def render_builtin_prompt_profile(prompt_profile: str) -> str | None:
    normalized_prompt_profile = prompt_profile.strip()
    if not is_builtin_prompt_profile(normalized_prompt_profile):
        return None
    return _render_known_builtin_prompt_profile(normalized_prompt_profile)


def compose_prompt_with_user_append(
    generated: str | None,
    user_append: str | None,
) -> str:
    generated_text = generated.strip() if isinstance(generated, str) else ""
    user_text = user_append.strip() if isinstance(user_append, str) else ""
    if generated_text and user_text:
        return f"{generated_text}\n\n{_USER_APPEND_HEADING}\n{user_text}"
    if generated_text:
        return generated_text
    return user_text


def render_agent_prompt(materialization: AgentPromptMaterialization | None) -> str | None:
    if materialization is None:
        return None
    if materialization.source == "custom_markdown":
        assert materialization.body is not None
        return compose_prompt_with_user_append(materialization.body, materialization.prompt_append)
    builtin_prompt = render_builtin_prompt_profile(materialization.profile)
    if builtin_prompt is None:
        raise ValueError(f"unknown builtin agent prompt profile: {materialization.profile}")
    return builtin_prompt


__all__ = [
    "compose_prompt_with_user_append",
    "has_builtin_prompt_profile",
    "is_builtin_prompt_profile",
    "render_agent_prompt",
    "render_builtin_prompt_profile",
]
