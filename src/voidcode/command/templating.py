from __future__ import annotations

import shlex


def split_command_arguments(arguments: str) -> tuple[str, ...]:
    if not arguments.strip():
        return ()
    return tuple(shlex.split(arguments, posix=True))


def render_command_template(
    template: str, *, raw_arguments: str, arguments: tuple[str, ...]
) -> str:
    rendered = template.replace("$ARGUMENTS", raw_arguments)
    for index, value in enumerate(arguments, start=1):
        rendered = rendered.replace(f"${index}", value)
    # Unbound positional placeholders should render as empty strings rather than leaking
    # implementation details into the prompt.
    for index in range(len(arguments) + 1, 10):
        rendered = rendered.replace(f"${index}", "")
    return rendered.strip()
