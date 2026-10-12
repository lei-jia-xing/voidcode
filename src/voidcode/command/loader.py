from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..frontmatter import load_frontmatter_mapping, split_frontmatter
from .models import CommandDefinition, CommandSource
from .registry import CommandRegistry

_BUILTIN_COMMANDS: tuple[CommandDefinition, ...] = (
    CommandDefinition(
        name="init",
        description="Generate or refresh repository-specific AGENTS.md guidance.",
        template=(
            "Inspect this repository using its actual files, documentation, tests, and configuration. "
            "Generate concise, stable AGENTS.md guidance for this workspace.\n\n"
            "Preserve and merge useful existing guidance; remove only information shown to be stale or contradictory. "
            "Do not include secrets, transient task plans, chat history, or unsupported claims. "
            "Do not impose a fixed section list or generic coding persona.\n\n"
            "Write AGENTS.md through the normal runtime tools and respect approval. "
            "Read the final file back and verify that it reflects repository evidence.\n\n"
            "Focus: $ARGUMENTS"
        ),
        source="builtin",
    ),
)


_SOURCE_PRECEDENCE: tuple[CommandSource, ...] = ("builtin", "extension", "user", "project", "skill", "mcp")


def builtin_commands() -> tuple[CommandDefinition, ...]:
    return _BUILTIN_COMMANDS


def load_command_registry(
    *,
    workspace: Path,
    user_commands_dir: Path | None = None,
    extension_commands: Iterable[CommandDefinition] = (),
) -> CommandRegistry:
    registry = CommandRegistry()
    for command in _commands_by_precedence(
        workspace=workspace,
        user_commands_dir=user_commands_dir,
        extension_commands=extension_commands,
    ):
        registry.register(command)
    return registry


def _commands_by_precedence(
    *,
    workspace: Path,
    user_commands_dir: Path | None,
    extension_commands: Iterable[CommandDefinition],
) -> Iterable[CommandDefinition]:
    yield from builtin_commands()
    yield from extension_commands
    if user_commands_dir is not None:
        yield from load_markdown_commands(user_commands_dir, source="user")
    yield from load_markdown_commands(workspace / "commands", source="project")
    yield from load_markdown_commands(workspace / ".voidcode" / "commands", source="project")


def load_markdown_commands(directory: Path, *, source: CommandSource) -> tuple[CommandDefinition, ...]:
    if source not in _SOURCE_PRECEDENCE:
        raise ValueError(f"unsupported command source: {source}")
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ValueError(f"command path is not a directory: {directory}")

    commands: list[CommandDefinition] = []
    for path in sorted(directory.rglob("*.md")):
        if not path.is_file():
            continue
        commands.append(_load_markdown_command(path, root=directory, source=source))
    return tuple(commands)


def _load_markdown_command(path: Path, *, root: Path, source: CommandSource) -> CommandDefinition:
    text = path.read_text(encoding="utf-8")
    raw_frontmatter, template = split_frontmatter(
        text,
        require_delimiter=False,
        require_closing=False,
        strip_body=False,
        source=str(path),
    )
    metadata = load_frontmatter_mapping(raw_frontmatter, source=str(path))
    if "mode" in metadata:
        raise ValueError(f"command {path} uses unsupported frontmatter field: mode")
    if raw_frontmatter is not None and not template.strip():
        raise ValueError(f"command {path} declares frontmatter but has no template body")
    relative_name = path.relative_to(root).with_suffix("").as_posix()
    raw_name = metadata.get("name", relative_name)
    description = metadata.get("description")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError(f"command {path} has invalid name")
    if not isinstance(description, str) or not description.strip():
        description = raw_name.strip()
    return CommandDefinition(
        name=raw_name,
        description=description.strip(),
        template=template,
        source=source,
        agent=_require_optional_string(metadata.get("agent")),
        model=_require_optional_string(metadata.get("model")),
        subtask=_metadata_bool(metadata.get("subtask"), default=False),
        enabled=_metadata_bool(metadata.get("enabled"), default=True),
        hidden=_metadata_bool(metadata.get("hidden"), default=False),
        path=path,
    )


def _require_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("command optional string frontmatter values must be non-empty strings")


def _metadata_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ValueError("command boolean frontmatter values must be booleans")
