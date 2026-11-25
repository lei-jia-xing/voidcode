"""Stable task identities for the todo plugin."""

from __future__ import annotations

import re
from uuid import uuid4

TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def new_task_id() -> str:
    return uuid4().hex


def valid_task_id(value: object) -> bool:
    return isinstance(value, str) and bool(TASK_ID_PATTERN.fullmatch(value))
