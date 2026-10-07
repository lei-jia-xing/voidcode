from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..hook.config import RuntimeHooksConfig
from ..tools.apply_patch import ApplyPatchTool
from ..tools.apply_workspace_edit import ApplyWorkspaceEditTool
from ..tools.ast_grep import AstGrepTool
from ..tools.contracts import Tool, ToolDefinition
from ..tools.delegation import TaskBatchTool, TaskTool
from ..tools.edit import EditTool
from ..tools.glob import GlobTool
from ..tools.grep import GrepTool
from ..tools.invoke_tool import InvokeTool
from ..tools.lsp import LspTool
from ..tools.multi_edit import MultiEditTool
from ..tools.process.background_process import BackgroundProcessTool
from ..tools.question import QuestionTool
from ..tools.read import ReadTool
from ..tools.shell_exec import ShellExecTool
from ..tools.skill import SkillTool
from ..tools.todo import TodoTool
from ..tools.web_fetch import WebFetchTool
from ..tools.web_search import WebSearchTool
from ..tools.write import WriteTool
from ..tools.yield_tool import YieldTool
from .config import RuntimeAgentConfig

if TYPE_CHECKING:
    from .tool_registry import ToolRegistry

# Each row references the real definition owner and defers genuine construction.
_BUILTIN_TOOLS: tuple[tuple[ToolDefinition, Callable[[RuntimeHooksConfig | None], Tool]], ...] = (
    (ApplyWorkspaceEditTool.definition, lambda _: ApplyWorkspaceEditTool()),
    (EditTool.definition, lambda hooks: EditTool(hooks_config=hooks)),
    (GlobTool.definition, lambda _: GlobTool()),
    (GrepTool.definition, lambda _: GrepTool()),
    (InvokeTool.definition, lambda _: InvokeTool()),
    (ReadTool.definition, lambda _: ReadTool()),
    (ShellExecTool.definition, lambda _: ShellExecTool()),
    (YieldTool.definition, lambda _: YieldTool()),
    (WebFetchTool.definition, lambda _: WebFetchTool()),
    (WebSearchTool.definition, lambda _: WebSearchTool()),
    (WriteTool.definition, lambda hooks: WriteTool(hooks_config=hooks)),
    (LspTool.definition, lambda _: LspTool()),
    (SkillTool.definition, lambda _: SkillTool()),
    (TaskTool.definition, lambda _: TaskTool()),
    (TaskBatchTool.definition, lambda _: TaskBatchTool()),
    (QuestionTool.definition, lambda _: QuestionTool()),
    (BackgroundProcessTool.definition, lambda _: BackgroundProcessTool()),
    (ApplyPatchTool.definition, lambda hooks: ApplyPatchTool(hooks_config=hooks)),
    (AstGrepTool.definition, lambda _: AstGrepTool()),
    (MultiEditTool.definition, lambda hooks: MultiEditTool(hooks_config=hooks)),
    (TodoTool.definition, lambda _: TodoTool()),
)


def builtin_tool_definitions() -> tuple[ToolDefinition, ...]:
    """Supported native declarations; selection and authorization belong to the root."""
    return tuple(definition for definition, _ in _BUILTIN_TOOLS)


def materialize_builtin_tool(
    tool_name: str,
    *,
    hooks_config: RuntimeHooksConfig | None = None,
) -> Tool:
    """Construct one selected native tool after the caller's activation gate."""
    for definition, factory in _BUILTIN_TOOLS:
        if definition.name == tool_name:
            return factory(hooks_config)
    raise ValueError(f"unknown builtin tool: {tool_name}")


BUILTIN_TOOL_NAMES = frozenset(definition.name for definition, _ in _BUILTIN_TOOLS)


def scoped_tool_registry_for_agent(
    registry: ToolRegistry,
    *,
    agent: RuntimeAgentConfig | None,
    builtin_mcp_tool_names: Iterable[str] = (),
) -> ToolRegistry:
    if agent is None:
        return registry

    scoped_registry = registry
    internal = agent.runtime_internal if agent is not None else None
    if internal is not None and internal.manifest_tool_allowlist:
        scoped_registry = scoped_registry.filtered(internal.manifest_tool_allowlist)

    if agent.tools is not None:
        if agent.tools.builtin is not None and agent.tools.builtin.enabled is False:
            scoped_registry = scoped_registry.excluding((*BUILTIN_TOOL_NAMES, *builtin_mcp_tool_names))
        if agent.tools.allowlist is not None:
            scoped_registry = scoped_registry.filtered(agent.tools.allowlist)
        if agent.tools.default is not None:
            scoped_registry = scoped_registry.filtered(agent.tools.default)

    return scoped_registry
