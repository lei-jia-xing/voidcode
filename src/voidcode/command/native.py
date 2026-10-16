from __future__ import annotations

from typing import Final

from .models import NativeCommand

INIT_PROMPT: Final[str] = (
    "Generate a file named AGENTS.md that serves as a contributor guide for this repository.\n"
    "Before writing, check whether AGENTS.md already exists in the current working directory. If it does, do not overwrite or modify it.\n"
    "Your goal is to produce a clear, concise, and well-structured document with descriptive headings and actionable explanations for each section.\n"
    "Follow the outline below, but adapt as needed — add sections if relevant, and omit those that do not apply to this project.\n\n"
    "Document Requirements\n\n"
    '- Title the document "Repository Guidelines".\n'
    "- Use Markdown headings (#, ##, etc.) for structure.\n"
    "- Keep the document concise. 200-400 words is optimal.\n"
    "- Keep explanations short, direct, and specific to this repository.\n"
    "- Provide examples where helpful (commands, directory paths, naming patterns).\n"
    "- Maintain a professional, instructional tone.\n\n"
    "Recommended Sections\n\n"
    "Project Structure & Module Organization\n\n"
    "- Outline the project structure, including where the source code, tests, and assets are located.\n\n"
    "Build, Test, and Development Commands\n\n"
    "- List key commands for building, testing, and running locally (e.g., npm test, make build).\n"
    "- Briefly explain what each command does.\n\n"
    "Coding Style & Naming Conventions\n\n"
    "- Specify indentation rules, language-specific style preferences, and naming patterns.\n"
    "- Include any formatting or linting tools used.\n\n"
    "Testing Guidelines\n\n"
    "- Identify testing frameworks and coverage requirements.\n"
    "- State test naming conventions and how to run tests.\n\n"
    "Commit & Pull Request Guidelines\n\n"
    "- Summarize commit message conventions found in the project’s Git history.\n"
    "- Outline pull request requirements (descriptions, linked issues, screenshots, etc.).\n\n"
    "(Optional) Add other sections if relevant, such as Security & Configuration Tips, Architecture Overview, or Agent-Specific Instructions."
)


# Authoritative single table of all native actions, pickers, and modes.
# Catalog generation, TUI validation/dispatch, and client menus derive from this table.
NATIVE_COMMANDS: Final[tuple[NativeCommand, ...]] = (
    NativeCommand(
        name="!",
        description="Prefix a command with ! to run it locally",
        arg_rule="required_text",
        busy_gate=False,
        kind="action",
    ),
    NativeCommand(
        name="btw",
        description="ask a quick side question without interrupting the main conversation",
        arg_rule="optional_text",
        busy_gate=False,
        kind="action",
    ),
    NativeCommand(
        name="clear",
        description="clear visible transcript and start a fresh session",
        arg_rule="optional_text",
        busy_gate=True,
        kind="action",
    ),
    NativeCommand(
        name="compact",
        description="summarize and persist the current conversation context",
        arg_rule="none",
        busy_gate=True,
        kind="action",
    ),
    NativeCommand(
        name="context",
        description="visualize current context-window usage",
        arg_rule="enum",
        busy_gate=False,
        kind="action",
        allowed_args=("", "all"),
    ),
    NativeCommand(
        name="copy",
        description="copy from the last assistant response",
        arg_rule="none",
        busy_gate=False,
        kind="picker",
    ),
    NativeCommand(
        name="diff",
        description="show the aggregate Git diff, including untracked files",
        arg_rule="none",
        busy_gate=False,
        kind="action",
    ),
    NativeCommand(
        name="export",
        description="export the session transcript as Markdown",
        arg_rule="optional_text",
        busy_gate=True,
        kind="picker",
    ),
    NativeCommand(
        name="fork",
        description="duplicate current session carrying model and settings",
        arg_rule="optional_text",
        busy_gate=True,
        kind="action",
    ),
    NativeCommand(
        name="goal",
        description="set, inspect, or manage the current session goal",
        arg_rule="optional_text",
        busy_gate=False,
        kind="action",
    ),
    NativeCommand(
        name="init",
        description="create an AGENTS.md contributor guide for this repository",
        arg_rule="none",
        busy_gate=True,
        kind="action",
    ),
    NativeCommand(
        name="new",
        description="start fresh session in place",
        arg_rule="optional_text",
        busy_gate=True,
        kind="action",
    ),
    NativeCommand(
        name="plan",
        description="enter reversible read-only Plan mode",
        arg_rule="enum",
        busy_gate=True,
        kind="mode",
        allowed_args=("", "off"),
    ),
    NativeCommand(
        name="rename",
        description="rename the current session",
        arg_rule="optional_text",
        busy_gate=False,
        kind="action",
    ),
    NativeCommand(
        name="resume",
        description="resume a saved session by id or name, or open session picker",
        arg_rule="optional_text",
        busy_gate=True,
        kind="picker",
    ),
    NativeCommand(
        name="review",
        description="review current workspace changes with the governed review agent",
        arg_rule="optional_text",
        busy_gate=True,
        kind="picker",
    ),
    NativeCommand(
        name="status",
        description="show session, model, and usage status",
        arg_rule="none",
        busy_gate=False,
        kind="action",
    ),
)

NATIVE_COMMANDS_BY_NAME: Final[dict[str, NativeCommand]] = {cmd.name: cmd for cmd in NATIVE_COMMANDS}
RESERVED_NATIVE_NAMES: Final[frozenset[str]] = frozenset(cmd.name for cmd in NATIVE_COMMANDS)


def is_native_command(name: str) -> bool:
    """Return True if ``name`` (without leading slash) is a reserved native command."""
    return name in RESERVED_NATIVE_NAMES
