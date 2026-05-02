from __future__ import annotations

from collections.abc import Callable, Iterable
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Protocol

from ..agent import get_builtin_agent_manifest
from ..hook.config import RuntimeHooksConfig
from ..skills.models import SkillMetadata
from ..tools.contracts import Tool
from ..tools.edit import EditTool
from ..tools.glob import GlobTool
from ..tools.grep import GrepTool
from ..tools.list_dir import ListTool
from ..tools.local_custom import discover_local_custom_tools
from ..tools.read_file import ReadFileTool
from ..tools.shell_exec import ShellExecTool
from ..tools.web_fetch import WebFetchTool
from ..tools.web_search import WebSearchTool
from ..tools.write_file import WriteFileTool
from .config import RuntimeAgentConfig, RuntimeToolsLocalConfig

BUILTIN_TOOL_NAMES = frozenset(
    {
        "apply_patch",
        "ast_grep_preview",
        "ast_grep_replace",
        "ast_grep_search",
        "background_cancel",
        "background_output",
        "code_search",
        "edit",
        "format_file",
        "glob",
        "grep",
        "list",
        "lsp",
        "multi_edit",
        "read_file",
        "question",
        "shell_exec",
        "skill",
        "task",
        "todo_write",
        "web_fetch",
        "web_search",
        "write_file",
    }
)


class ScopedToolRegistry(Protocol):
    tools: dict[str, Tool]

    def filtered[ToolRegistryT: "ScopedToolRegistry"](
        self: ToolRegistryT,
        patterns: Iterable[str],
    ) -> ToolRegistryT: ...

    def excluding[ToolRegistryT: "ScopedToolRegistry"](
        self: ToolRegistryT,
        tool_names: Iterable[str],
    ) -> ToolRegistryT: ...


def scoped_tool_registry_for_agent[ToolRegistryT: ScopedToolRegistry](
    registry: ToolRegistryT,
    *,
    agent: RuntimeAgentConfig | None,
) -> ToolRegistryT:
    if agent is None:
        return registry

    scoped_registry = registry
    manifest = get_builtin_agent_manifest(agent.preset)
    if manifest is not None and manifest.tool_allowlist:
        scoped_registry = scoped_registry.filtered(manifest.tool_allowlist)

    if agent.tools is not None:
        if agent.tools.builtin is not None and agent.tools.builtin.enabled is False:
            scoped_registry = scoped_registry.excluding(BUILTIN_TOOL_NAMES)
        if agent.tools.allowlist is not None:
            scoped_registry = scoped_registry.filtered(agent.tools.allowlist)
        if agent.tools.default is not None:
            scoped_registry = scoped_registry.filtered(agent.tools.default)

    return scoped_registry


def tool_name_matches_patterns(tool_name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(tool_name, pattern) for pattern in patterns if pattern)


class _NoArgToolFactory(Protocol):
    def __call__(self) -> Tool: ...


class _HookedToolFactory(Protocol):
    def __call__(self, *, hooks_config: RuntimeHooksConfig | None = None) -> Tool: ...


class _SkillToolFactory(Protocol):
    def __call__(
        self,
        *,
        list_skills: Callable[[], tuple[SkillMetadata, ...]],
        resolve_skill: Callable[[str], SkillMetadata],
    ) -> Tool: ...


# Import optional tools independently so one failure doesn't hide others.
try:
    from ..tools.apply_patch import ApplyPatchTool
except ImportError:
    _ApplyPatchTool: _HookedToolFactory | None = None
else:
    _ApplyPatchTool: _HookedToolFactory | None = ApplyPatchTool

try:
    from ..tools.ast_grep import AstGrepPreviewTool, AstGrepReplaceTool, AstGrepSearchTool
except ImportError:
    _AstGrepPreviewTool: _NoArgToolFactory | None = None
    _AstGrepReplaceTool: _NoArgToolFactory | None = None
    _AstGrepSearchTool: _NoArgToolFactory | None = None
else:
    _AstGrepPreviewTool: _NoArgToolFactory | None = AstGrepPreviewTool
    _AstGrepReplaceTool: _NoArgToolFactory | None = AstGrepReplaceTool
    _AstGrepSearchTool: _NoArgToolFactory | None = AstGrepSearchTool

try:
    from ..tools.code_search import CodeSearchTool
except ImportError:
    _CodeSearchTool: _NoArgToolFactory | None = None
else:
    _CodeSearchTool: _NoArgToolFactory | None = CodeSearchTool

try:
    from ..tools.multi_edit import MultiEditTool
except ImportError:
    _MultiEditTool: _HookedToolFactory | None = None
else:
    _MultiEditTool: _HookedToolFactory | None = MultiEditTool

try:
    from ..tools.question import QuestionTool
except ImportError:
    _QuestionTool: _NoArgToolFactory | None = None
else:
    _QuestionTool: _NoArgToolFactory | None = QuestionTool

try:
    from ..tools.skill import SkillTool
except ImportError:
    _SkillTool: _SkillToolFactory | None = None
else:
    _SkillTool: _SkillToolFactory | None = SkillTool

try:
    from ..tools.todo_write import TodoWriteTool
except ImportError:
    _TodoWriteTool: _NoArgToolFactory | None = None
else:
    _TodoWriteTool: _NoArgToolFactory | None = TodoWriteTool


class ToolProvider(Protocol):
    def provide_tools(self) -> tuple[Tool, ...]: ...


class LocalCustomToolProvider:
    def __init__(self, *, workspace: Path, config: RuntimeToolsLocalConfig | None) -> None:
        self._workspace = workspace
        self._config = config

    def provide_tools(self) -> tuple[Tool, ...]:
        if self._config is None:
            return ()
        if not self._config.path:
            raise ValueError("local custom tools path must not be empty")
        relative_path = Path(self._config.path)
        if relative_path.is_absolute():
            raise ValueError("local custom tools path must be workspace-relative")
        if ".." in relative_path.parts:
            raise ValueError("local custom tools path must not contain '..'")
        return discover_local_custom_tools(
            self._workspace,
            enabled=self._config.enabled,
            relative_path=str(relative_path),
        )


class BuiltinToolProvider:
    _lsp_tool: Tool | None
    _format_tool: Tool | None
    _mcp_tools: tuple[Tool, ...]
    _hooks_config: RuntimeHooksConfig | None
    _skill_tool: Tool | None
    _task_tool: Tool | None
    _question_tool: Tool | None
    _background_output_tool: Tool | None
    _background_cancel_tool: Tool | None

    def __init__(
        self,
        *,
        lsp_tool: Tool | None = None,
        format_tool: Tool | None = None,
        mcp_tools: tuple[Tool, ...] = (),
        hooks_config: RuntimeHooksConfig | None = None,
        skill_tool: Tool | None = None,
        task_tool: Tool | None = None,
        question_tool: Tool | None = None,
        background_output_tool: Tool | None = None,
        background_cancel_tool: Tool | None = None,
    ) -> None:
        self._lsp_tool = lsp_tool
        self._format_tool = format_tool
        self._mcp_tools = mcp_tools
        self._hooks_config = hooks_config
        self._skill_tool = skill_tool
        self._task_tool = task_tool
        self._question_tool = question_tool
        self._background_output_tool = background_output_tool
        self._background_cancel_tool = background_cancel_tool

    def provide_tools(self) -> tuple[Tool, ...]:
        edit_tool = EditTool(hooks_config=self._hooks_config)
        tools: list[Tool] = [
            edit_tool,
            GlobTool(),
            GrepTool(),
            ListTool(),
            ReadFileTool(),
            ShellExecTool(),
            WebFetchTool(),
            WebSearchTool(),
            WriteFileTool(hooks_config=self._hooks_config),
        ]

        if self._lsp_tool is not None:
            tools.append(self._lsp_tool)
        if self._format_tool is not None:
            tools.append(self._format_tool)

        if self._skill_tool is not None:
            tools.append(self._skill_tool)
        elif _SkillTool is not None:
            tools.append(_SkillTool(list_skills=lambda: (), resolve_skill=self._unknown_skill))

        if self._task_tool is not None:
            tools.append(self._task_tool)

        if self._question_tool is not None:
            tools.append(self._question_tool)
        elif _QuestionTool is not None:
            tools.append(_QuestionTool())

        if self._background_output_tool is not None:
            tools.append(self._background_output_tool)

        if self._background_cancel_tool is not None:
            tools.append(self._background_cancel_tool)

        tools.extend(self._mcp_tools)

        # Add optional tools if available.
        if _ApplyPatchTool is not None:
            tools.append(_ApplyPatchTool(hooks_config=self._hooks_config))
        if _AstGrepSearchTool is not None:
            tools.append(_AstGrepSearchTool())
        if _AstGrepPreviewTool is not None:
            tools.append(_AstGrepPreviewTool())
        if _AstGrepReplaceTool is not None:
            tools.append(_AstGrepReplaceTool())
        if _CodeSearchTool is not None:
            tools.append(_CodeSearchTool())
        if _MultiEditTool is not None:
            tools.append(_MultiEditTool(hooks_config=self._hooks_config))
        if _TodoWriteTool is not None:
            tools.append(_TodoWriteTool())

        return tuple(tools)

    @staticmethod
    def _unknown_skill(name: str) -> SkillMetadata:
        raise ValueError(f"unknown skill: {name}")
