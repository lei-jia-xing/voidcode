"""Pure command-catalog projections shared by every help surface.

The catalog is the only authority: help text, argument hints, grouping, and
examples are derived from :class:`~voidcode.sdk.commands.CommandDescriptor`
fields. Clients that need a different layout consume these entries; they never
maintain a parallel command list.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .commands import CommandDescriptor

__all__ = [
    "CommandHelpEntry",
    "command_argument_hints",
    "command_usage",
    "grouped_help_entries",
    "help_entries",
    "public_command_path",
]


@dataclass(frozen=True, slots=True)
class CommandHelpEntry:
    """One visible catalog command with its derived help presentation."""

    name: str
    group: str
    path: str
    usage: str
    description: str
    examples: tuple[str, ...]
    shortcut: str | None


def public_command_path(descriptor: CommandDescriptor) -> str:
    """Public slash spelling without the leading slash (``goal.set`` → ``goal set``)."""
    syntax = descriptor.client_syntax
    if syntax and syntax.startswith("/"):
        path = " ".join(syntax[1:].strip().replace(".", " ").split())
        if path:
            return path
    return " ".join(descriptor.name.replace(".", " ").split()) or descriptor.name


def command_argument_hints(descriptor: CommandDescriptor) -> tuple[str, ...]:
    """Declared argument names in schema order, required first-class and optional bracketed."""
    properties = descriptor.argument_schema.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    raw_required = descriptor.argument_schema.get("required")
    required = {name for name in raw_required if isinstance(name, str)} if isinstance(raw_required, (list, tuple, set, frozenset)) else set()

    hints: list[str] = []
    for name in properties:
        if not isinstance(name, str) or not name:
            continue
        hints.append(f"<{name}>" if name in required else f"[{name}]")
    if descriptor.text_argument and descriptor.text_argument not in properties:
        hints.append(f"<{descriptor.text_argument}>")
    return tuple(hints)


def command_usage(descriptor: CommandDescriptor) -> str:
    """Full client usage line: explicit metadata wins, else path plus schema arguments."""
    if descriptor.usage and descriptor.usage.strip():
        return descriptor.usage.strip()
    usage = f"/{public_command_path(descriptor)}"
    hints = command_argument_hints(descriptor)
    if hints:
        return f"{usage} {' '.join(hints)}"
    if descriptor.has_input:
        return f"{usage} …"
    if descriptor.argument_schema and not isinstance(descriptor.argument_schema.get("properties"), Mapping):
        return f"{usage} …"
    return usage


def _help_group(descriptor: CommandDescriptor) -> str:
    if descriptor.group and descriptor.group.strip():
        return descriptor.group.strip()
    owner = descriptor.owner.strip()
    if owner == "core":
        return "Core"
    return owner.replace("_", " ") or "Other"


def help_entries(descriptors: Sequence[CommandDescriptor]) -> tuple[CommandHelpEntry, ...]:
    """Visible commands as deterministic, group-ordered help entries.

    ``slash_visible=False`` commands are internal UI helpers: still callable,
    never advertised. Groups sort ``Core`` first, then by label; commands sort
    by their public usage line inside each group.
    """
    entries = [
        CommandHelpEntry(
            name=descriptor.name,
            group=_help_group(descriptor),
            path=public_command_path(descriptor),
            usage=command_usage(descriptor),
            description=descriptor.description,
            examples=tuple(descriptor.examples),
            shortcut=descriptor.default_shortcut,
        )
        for descriptor in descriptors
        if descriptor.slash_visible
    ]
    return tuple(sorted(entries, key=lambda entry: (entry.group != "Core", entry.group.casefold(), entry.usage.casefold())))


def grouped_help_entries(descriptors: Sequence[CommandDescriptor]) -> tuple[tuple[str, tuple[CommandHelpEntry, ...]], ...]:
    """``help_entries`` folded into consecutive ``(group, entries)`` pairs."""
    groups: list[tuple[str, list[CommandHelpEntry]]] = []
    for entry in help_entries(descriptors):
        if groups and groups[-1][0] == entry.group:
            groups[-1][1].append(entry)
        else:
            groups.append((entry.group, [entry]))
    return tuple((group, tuple(entries)) for group, entries in groups)
