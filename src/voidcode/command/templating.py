from __future__ import annotations

import re
import shlex

_PLACEHOLDER_PATTERN = re.compile(r"\$(ARGUMENTS(?![A-Za-z0-9_])|[1-9](?!\d))")


def split_command_arguments(arguments: str) -> tuple[str, ...]:
    if not arguments.strip():
        return ()
    return tuple(shlex.split(arguments, posix=True))


def render_command_template(
    template: str, *, raw_arguments: str, arguments: tuple[str, ...]
) -> str:
    def replacement(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "ARGUMENTS":
            return raw_arguments
        index = int(token) - 1
        if index >= len(arguments):
            return ""
        return arguments[index]

    return _PLACEHOLDER_PATTERN.sub(replacement, template).strip()
