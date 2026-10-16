from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

NativeArgRule = Literal["none", "optional_text", "required_text", "enum"]
NativeCommandKind = Literal["action", "picker", "mode"]


@dataclass(frozen=True, slots=True)
class NativeCommand:
    name: str
    description: str
    arg_rule: NativeArgRule
    busy_gate: bool
    kind: NativeCommandKind
    allowed_args: tuple[str, ...] = ()
