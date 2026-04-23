from __future__ import annotations

from typing import Protocol

from ..hook.config import RuntimeHooksConfig
from ..skills.models import SkillMetadata
from ..tools.contracts import Tool
from ..tools.edit import EditTool
from ..tools.glob import GlobTool
from ..tools.grep import GrepTool
from ..tools.list_dir import ListTool
from ..tools.read_file import ReadFileTool
from ..tools.shell_exec import ShellExecTool
from ..tools.web_fetch import WebFetchTool
from ..tools.web_search import WebSearchTool
from ..tools.write_file import WriteFileTool

# Import optional tools independently so one failure doesn't hide others.
try:
    from ..tools.apply_patch import ApplyPatchTool as _ApplyPatchTool
except ImportError:
    _ApplyPatchTool = None

try:
    from ..tools.ast_grep import AstGrepPreviewTool as _AstGrepPreviewTool
    from ..tools.ast_grep import AstGrepReplaceTool as _AstGrepReplaceTool
    from ..tools.ast_grep import AstGrepSearchTool as _AstGrepSearchTool
except ImportError:
    _AstGrepPreviewTool = None
    _AstGrepReplaceTool = None
    _AstGrepSearchTool = None

try:
    from ..tools.code_search import CodeSearchTool as _CodeSearchTool
except ImportError:
    _CodeSearchTool = None

try:
    from ..tools.multi_edit import MultiEditTool as _MultiEditTool
except ImportError:
    _MultiEditTool = None

try:
    from ..tools.question import QuestionTool as _QuestionTool
except ImportError:
    _QuestionTool = None

try:
    from ..tools.skill import SkillTool as _SkillTool
except ImportError:
    _SkillTool = None

try:
    from ..tools.todo_write import TodoWriteTool as _TodoWriteTool
except ImportError:
    _TodoWriteTool = None


class ToolProvider(Protocol):
    def provide_tools(self) -> tuple[Tool, ...]: ...


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
            WriteFileTool(),
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
            tools.append(_ApplyPatchTool())
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
