from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str
    directory: Path
    entry_path: Path
    content: str
