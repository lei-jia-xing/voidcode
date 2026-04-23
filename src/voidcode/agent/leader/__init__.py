from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Mapping

_LEADER_DIR = Path(__file__).resolve().parent


@cache
def _load_prompt_text(filename: str) -> str:
    return (_LEADER_DIR / filename).read_text(encoding="utf-8").strip()


def render_leader_prompt(agent_preset: Mapping[str, object] | None = None) -> str:
    _ = agent_preset
    return _load_prompt_text("base.txt")


__all__ = ["render_leader_prompt"]
