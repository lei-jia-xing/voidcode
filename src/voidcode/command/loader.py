from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .models import CommandDefinition, CommandSource
from .registry import CommandRegistry

_BUILTIN_COMMANDS: tuple[CommandDefinition, ...] = (
    CommandDefinition(
        name="commit",
        description="Analyze staged changes and generate a Conventional Commits message.",
        template=(
            "Workflow: inspect current git status and diff, then draft a focused Conventional "
            "Commits message using the repository's style. Arguments are optional context for "
            "the intended commit: $ARGUMENTS. Read-only by default: do not create commits, "
            "amend history, or push unless explicitly instructed. Verification: if there are "
            "no staged or unstaged changes, report that clearly instead of inventing a message."
        ),
        source="builtin",
    ),
    CommandDefinition(
        name="explain",
        description="Explain code, concepts, architecture, or errors with clarity.",
        template=(
            "Workflow: read the requested target, error, stack trace, or behavior and explain it "
            "with concrete examples where helpful. Arguments identify the target to explain: "
            "$ARGUMENTS. Read-only by default: do not modify any files or run destructive "
            "commands. Verification: if the target cannot be found or read, say so clearly "
            "and do not hallucinate file contents."
        ),
        source="builtin",
    ),
    CommandDefinition(
        name="fix",
        description="Fix a code issue or bug with a concrete, verifiable patch.",
        template=(
            "Workflow: locate the root cause, make the smallest safe code change, and verify "
            "the fix. Arguments describe the concrete problem to diagnose: $ARGUMENTS. "
            "Execution mode: editing is allowed when needed, but preserve runtime/tool/approval "
            "governance. Verification: run targeted tests, lint, or type checks that cover the "
            "fix; if verification still fails, report the remaining failure clearly."
        ),
        source="builtin",
    ),
    CommandDefinition(
        name="plan",
        description="Create an implementation plan before writing code.",
        template=(
            "Workflow: produce an implementation plan, acceptance criteria, risks, and a "
            "verification strategy for the requested goal. Arguments describe the planning "
            "target: $ARGUMENTS. Target agent: product. Read-only by default: do not write "
            "code or modify files unless explicitly instructed after the plan is accepted. "
            "Use todo_write only for session planning/progress state; it is runtime state, not "
            "workspace mutation. If this plan should be executed later, include a concise "
            "Start-work handoff section with the exact goal, files/modules, verification, and "
            "open risks."
        ),
        source="builtin",
        agent="product",
        workflow_preset="review",
    ),
    CommandDefinition(
        name="start-work",
        description="Start implementation from a previously accepted plan or handoff.",
        template=(
            "Workflow: execute the accepted plan or handoff using runtime tools. Arguments "
            "identify the plan text, plan file, plan session id, issue, or goal to implement: "
            "$ARGUMENTS. If a plan session id is provided, use the runtime-hydrated plan "
            "artifact included below as the source of truth. First restate the concrete "
            "implementation target and constraints, then "
            "make the smallest safe changes. Use todo_write for multi-step progress tracking, "
            "but do not treat todos as the durable plan artifact. Verification: run targeted "
            "checks that cover the changed behavior and report any unverified risk."
        ),
        source="builtin",
        workflow_preset="implementation",
    ),
    CommandDefinition(
        name="review",
        description="Review the requested code or change set.",
        template=(
            "Workflow: review the requested file, directory, diff, PR, or current changes and "
            "report findings by severity with actionable fixes. Arguments identify the review "
            "target; if empty, review the current working tree changes: $ARGUMENTS. Read-only "
            "by default: do not modify files. Verification: if the target is empty, missing, "
            "or unreadable, explain that instead of producing a generic review."
        ),
        source="builtin",
    ),
    CommandDefinition(
        name="test",
        description="Generate and/or run tests with verification guidance.",
        template=(
            "Workflow: add focused tests, run relevant tests, or explain test failures for the "
            "requested target. Arguments identify the code or behavior under test: $ARGUMENTS. "
            "Execution mode: editing test files is allowed when adding coverage, but do not "
            "delete or weaken existing tests. Verification: prefer targeted test commands before "
            "broad suites; if no test framework is detected, explain the setup steps."
        ),
        source="builtin",
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
    for command in _commands_by_precedence(
        workspace=workspace, user_commands_dir=user_commands_dir
    ):
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


def load_markdown_commands(
    directory: Path, *, source: CommandSource
) -> tuple[CommandDefinition, ...]:
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
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
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
