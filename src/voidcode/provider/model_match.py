"""The one model matcher: how a data row selects the models it applies to.

Mirrors OMP's ``matchesList`` (``packages/catalog/src/compat/behavior.ts:15-26``)
so every data table in this package -- ``api_routes.json`` and
``thinking_rules.json`` -- selects models by the same rules: ``exact``/``prefix``/
``substring`` compare the raw id, ``token``/``glob`` compare the lowercased id,
and a row may carry several values of one kind.
"""

from __future__ import annotations

import re
from typing import Final, Literal

type Matcher = Literal["exact", "prefix", "substring", "token", "glob"]

MATCHERS: Final[tuple[Matcher, ...]] = ("exact", "prefix", "substring", "token", "glob")

_TOKEN_SEPARATOR = re.compile(r"[^a-z0-9]+")


def glob_match(pattern: str, value: str) -> bool:
    """Anchored ``*`` matching, byte-for-byte OMP's ``globMatch`` (``cascade.ts:57-75``)."""
    segments = pattern.split("*")
    if len(segments) == 1:
        return value == pattern
    head = segments[0]
    if not value.startswith(head):
        return False
    remainder = value[len(head) :]
    for segment in segments[1:-1]:
        if not segment:
            continue
        found = remainder.find(segment)
        if found == -1:
            return False
        remainder = remainder[found + len(segment) :]
    tail = segments[-1]
    return tail == "" or remainder.endswith(tail)


def matches(matcher: Matcher, value: str, model: str) -> bool:
    """Whether one matcher/value pair selects ``model``."""
    match matcher:
        case "exact":
            return model == value
        case "prefix":
            return model.startswith(value)
        case "substring":
            return value in model
        case "token":
            return value in _TOKEN_SEPARATOR.split(model.lower())
        case "glob":
            return glob_match(value, model.lower())


__all__ = ["MATCHERS", "Matcher", "glob_match", "matches"]
