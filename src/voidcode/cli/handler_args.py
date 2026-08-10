"""Typed frozen argument shapes for CLI handler command groups.

Field names match the Click command param names. The ``command`` and
``<group>_command`` keys are carried for subcommand routing and defaulted
per command group.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunArgs:
    """Arguments for the ``run`` command."""

    request: str
    command: str = "run"
    workspace: Path = field(default_factory=Path.cwd)
    session_id: str | None = None
    approval_mode: str | None = None
    agent: str | None = None
    model: str | None = None
    skills: tuple[str, ...] = ()
    max_steps: int | None = None
    reasoning_effort: str | None = None
    show_thinking: bool = False
    json: bool = False
    trace: bool = False
    provider_stream: bool | None = None
    runtime_mode: str | None = None
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class SessionsArgs:
    """Arguments for the ``sessions`` command group."""

    sessions_command: str | None = None
    command: str = "sessions"
    workspace: Path = field(default_factory=Path.cwd)
    session_id: str | None = None
    approval_request_id: str | None = None
    approval_decision: str | None = None
    dry_run: bool = False
    show_thinking: bool = False
    question_request_id: str | None = None
    response: tuple[str, ...] = ()
    response_json: str | None = None
    json: bool = False
    output: Path | None = None
    format: str = "zip"
    redact: bool = True
    include_tool_output: bool = False
    include_raw_provider_messages: bool = False
    include_reasoning_text: bool = False
    support: bool = False
    bundle_path: Path | None = None
    sequence: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryArgs:
    """Arguments for the ``memory`` command group."""

    memory_command: str | None = None
    command: str = "memory"
    workspace: Path = field(default_factory=Path.cwd)
    content: str | None = None
    kind: str | None = None
    tag: tuple[str, ...] = ()
    json: bool = False
    limit: int | None = None
    query: str | None = None
    memory_id: str | None = None


@dataclass(frozen=True, slots=True)
class TasksArgs:
    """Arguments for the ``tasks`` command group."""

    tasks_command: str | None = None
    command: str = "tasks"
    workspace: Path = field(default_factory=Path.cwd)
    task_id: str | None = None
    json: bool = False
    parent_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class StorageArgs:
    """Arguments for the ``storage`` command group."""

    storage_command: str | None = None
    command: str = "storage"
    workspace: Path = field(default_factory=Path.cwd)
    json: bool = False
    keep_sessions: int | None = None
    keep_background_tasks: int | None = None
    older_than: int | None = None


@dataclass(frozen=True, slots=True)
class ConfigArgs:
    """Arguments for the ``config`` command group."""

    config_command: str | None = None
    command: str = "config"
    workspace: Path = field(default_factory=Path.cwd)
    session_id: str | None = None
    json: bool = False
    approval_mode: str = "ask"
    model: str | None = None
    max_steps: int | None = None
    with_examples: bool = False
    print: bool = False
    force: bool = False


@dataclass(frozen=True, slots=True)
class ProviderArgs:
    """Arguments for the ``provider`` command group."""

    provider_command: str | None = None
    command: str = "provider"
    workspace: Path = field(default_factory=Path.cwd)
    provider: str | None = None
    refresh: bool = False


@dataclass(frozen=True, slots=True)
class CommandsArgs:
    """Arguments for the ``commands`` command group."""

    commands_command: str | None = None
    command: str = "commands"
    workspace: Path = field(default_factory=Path.cwd)
    user_commands_dir: Path | None = None
    include_hidden: bool = False
    include_disabled: bool = False
    json: bool = False
    name: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorArgs:
    """Arguments for the ``doctor`` command."""

    command: str = "doctor"
    workspace: Path = field(default_factory=Path.cwd)
    verbose: bool = False
    json: bool = False
    fix: bool = False
    model: str | None = None


@dataclass(frozen=True, slots=True)
class TuiArgs:
    """Arguments for the ``tui`` command."""

    command: str = "tui"
    workspace: Path = field(default_factory=Path.cwd)
    approval_mode: str | None = None


@dataclass(frozen=True, slots=True)
class AcpArgs:
    """Arguments for the ``acp`` command."""

    command: str = "acp"
    workspace: Path = field(default_factory=Path.cwd)
    approval_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ServerArgs:
    """Arguments for the ``serve`` and ``web`` commands."""

    command: str = "serve"
    workspace: Path = field(default_factory=Path.cwd)
    host: str = "127.0.0.1"
    port: int | None = None
    approval_mode: str | None = None
    server_entry: Callable[..., None] | None = None
    open_browser: bool = True


@dataclass(frozen=True, slots=True)
class AgentsArgs:
    """Arguments for the ``agents`` command group."""

    agents_command: str | None = None
    command: str = "agents"
    workspace: Path = field(default_factory=Path.cwd)
    json: bool = False


@dataclass(frozen=True, slots=True)
class McpArgs:
    """Arguments for the ``mcp`` command group."""

    mcp_command: str | None = None
    command: str = "mcp"
    workspace: Path = field(default_factory=Path.cwd)
    json: bool = False


__all__ = [
    "RunArgs",
    "SessionsArgs",
    "MemoryArgs",
    "TasksArgs",
    "StorageArgs",
    "ConfigArgs",
    "ProviderArgs",
    "CommandsArgs",
    "DoctorArgs",
    "TuiArgs",
    "AcpArgs",
    "ServerArgs",
    "AgentsArgs",
    "McpArgs",
]
