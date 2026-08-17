from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .models import CommandDefinition, CommandSource
from .registry import CommandRegistry

_BUILTIN_COMMANDS: tuple[CommandDefinition, ...] = (
    CommandDefinition(
        name="init",
        description="Generate or refresh a structured AGENTS.md project knowledge base.",
        template=(
            "Generate or refresh the project knowledge base (AGENTS.md) at the workspace root.\n"
            "\n"
            "Steps:\n"
            "1. Inspect the repository: structure, existing AGENTS.md, README/docs, build and test commands, and conventions.\n"
            "2. Write or update AGENTS.md with stable project knowledge only. Required sections: OVERVIEW, STRUCTURE, WHERE TO LOOK, "
            "CODE MAP, CONVENTIONS, ANTI-PATTERNS, UNIQUE STYLES, COMMANDS, NOTES.\n"
            "3. Preserve useful existing content; drop stale or duplicate guidance.\n"
            "\n"
            "Constraints:\n"
            "- Never store secrets, transient task plans, chat history, or unverified guesses.\n"
            "- Prefer concise tables and bullets over prose.\n"
            "\n"
            "Verify: read the final AGENTS.md and confirm it reflects the current repository without claiming unsupported capabilities.\n"
            "\n"
            "Focus notes: $ARGUMENTS"
        ),
        source="builtin",
    ),
    CommandDefinition(
        name="plan",
        description="Create an implementation plan before writing code.",
        template=(
            "Produce an implementation plan before writing code.\n"
            "\n"
            "Steps:\n"
            "1. Converge the request into a concrete goal with acceptance criteria, risks, and a verification strategy.\n"
            "2. Inspect the relevant code before committing to a shape.\n"
            "\n"
            "Constraints:\n"
            "- Read-only: do not write code or modify files unless explicitly instructed after the plan is accepted.\n"
            "- Plan directly, or delegate to the product agent (subagent_type=product) via the task tool and read its plan back.\n"
            "- todo_write is runtime state, not workspace mutation.\n"
            "\n"
            "Target: $ARGUMENTS"
        ),
        source="builtin",
        mode="plan",
    ),
)


_SOURCE_PRECEDENCE: tuple[CommandSource, ...] = ("builtin", "user", "project", "skill", "mcp")


def builtin_commands() -> tuple[CommandDefinition, ...]:
    return _BUILTIN_COMMANDS


def load_command_registry(
    *,
    workspace: Path,
    user_commands_dir: Path | None = None,
) -> CommandRegistry:
    registry = CommandRegistry()
    for command in _commands_by_precedence(workspace=workspace, user_commands_dir=user_commands_dir):
        registry.register(command)
    return registry


def _commands_by_precedence(
    *,
    workspace: Path,
    user_commands_dir: Path | None,
) -> Iterable[CommandDefinition]:
    yield from builtin_commands()
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
    metadata, template = _split_frontmatter(text)
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
        agent=_optional_string(metadata.get("agent")),
        mode=_optional_string(metadata.get("mode")),
        model=_optional_string(metadata.get("model")),
        subtask=_metadata_bool(metadata.get("subtask"), default=False),
        enabled=_metadata_bool(metadata.get("enabled"), default=True),
        hidden=_metadata_bool(metadata.get("hidden"), default=False),
        path=path,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw_frontmatter = text[4:end]
    body = text[end + len("\n---\n") :]
    return _parse_simple_frontmatter(raw_frontmatter), body


def _parse_simple_frontmatter(raw_frontmatter: str) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for line_number, line in enumerate(raw_frontmatter.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            raise ValueError(f"invalid command frontmatter line {line_number}: {line}")
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError(f"invalid command frontmatter line {line_number}: {line}")
        metadata[normalized_key] = _parse_scalar(value.strip())
    return metadata


def _parse_scalar(value: str) -> object:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _optional_string(value: object) -> str | None:
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
