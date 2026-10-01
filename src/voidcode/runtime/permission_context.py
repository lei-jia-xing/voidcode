from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..tools.contracts import Tool, ToolCall, ToolDefinition, ToolEffect, is_read_tier
from ..tools.local_custom import LocalCustomTool
from ..tools.mcp import McpTool
from .permission import OperationClass, PathScope

#: Builtin tools that mutate workspace/session state without executing code:
#: VoidCode's ``write`` tier. Everything else a non-read-only tool can be — a
#: shell, a spawned agent/process, a local custom command, an MCP call — is
#: ``execute`` (or, for MCP, ``write``), and an undeclared tool falls to the
#: safe ``execute`` default.
_WRITE_TIER_BUILTINS = frozenset({"write", "edit", "multi_edit", "apply_patch", "apply_workspace_edit"})


class RuntimePermissionContextResolver:
    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace

    def permission_context_for_tool_call(
        self,
        *,
        tool: ToolDefinition,
        tool_instance: Tool,
        tool_call: ToolCall,
        patch_path_extractor: Callable[[str], tuple[str, ...]],
    ) -> tuple[PathScope, str | None, OperationClass, tuple[str, ...]]:
        operation_class = operation_class_for_tool(
            tool_call.tool_name,
            tool.effects,
            tool_instance=tool_instance,
            arguments=tool_call.arguments,
        )
        candidate_paths = self.candidate_paths_for_tool_call(
            tool_call,
            tool=tool,
            patch_path_extractor=patch_path_extractor,
        )
        workspace_root = self._workspace.resolve()
        external_paths: list[str] = []
        for raw_path in candidate_paths:
            canonical = self.canonicalize_candidate_path(raw_path)
            if canonical is None:
                continue
            if canonical.is_relative_to(workspace_root):
                continue
            external_paths.append(str(canonical))
        if external_paths:
            return "external", external_paths[0], operation_class, tuple(external_paths)
        return "workspace", None, operation_class, ()

    def normalized_permission_path_candidates(
        self,
        tool_call: ToolCall,
        external_paths: tuple[str, ...],
        *,
        patch_path_extractor: Callable[[str], tuple[str, ...]],
        tool: ToolDefinition | None = None,
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for external_path in external_paths:
            normalized.append(Path(external_path).as_posix())
        for raw_path in self.candidate_paths_for_tool_call(
            tool_call,
            tool=tool,
            patch_path_extractor=patch_path_extractor,
        ):
            canonical = self.canonicalize_candidate_path(raw_path)
            if canonical is not None:
                normalized.append(canonical.as_posix())
            text = raw_path.strip().replace("\\", "/")
            if text:
                normalized.append(text)
        workspace_prefix = f"{self._workspace.as_posix().rstrip('/')}/"
        relative: list[str] = []
        for path in normalized:
            if path.startswith(workspace_prefix):
                relative.append(path.removeprefix(workspace_prefix))
        return tuple(dict.fromkeys((*relative, *normalized)))

    def candidate_paths_for_tool_call(
        self,
        tool_call: ToolCall,
        *,
        tool: ToolDefinition | None = None,
        patch_path_extractor: Callable[[str], tuple[str, ...]],
    ) -> tuple[str, ...]:
        arguments = tool_call.arguments
        candidates: list[str] = []

        if tool is not None:
            for key in tool.path_argument_keys:
                value = arguments.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value)

        if tool_call.tool_name == "apply_patch":
            patch_text = arguments.get("patch")
            if isinstance(patch_text, str) and patch_text:
                candidates.extend(patch_path_extractor(patch_text))
        return tuple(candidates)

    def canonicalize_candidate_path(self, raw_path: str) -> Path | None:
        text = raw_path.strip()
        if not text:
            return None
        try:
            candidate = Path(text).expanduser()
        except RuntimeError:
            candidate = Path(text)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        try:
            return candidate.resolve(strict=False)
        except OSError:
            return None


def operation_class_for_tool(
    tool_name: str,
    effects: frozenset[ToolEffect],
    *,
    tool_instance: Tool,
    arguments: dict[str, object] | None = None,
) -> OperationClass:
    """The approval tier for one call.

    Unknown tools (no declaration, and not a read-only tool) default to
    ``execute`` — the safe default shared with ``approval_decision``. A tool
    that declares itself read-only and whose arguments add no evidence stays
    ``read``.
    """
    if tool_name == "task":
        operation = arguments.get("operation") if arguments is not None else None
        if operation in ("output", "ps"):
            return "read"
        if operation == "steer":
            return "write"
        return "execute"
    if tool_name == "background_process":
        operation = arguments.get("op") if arguments is not None else None
        return "read" if operation in ("ps", "logs") else "execute"
    if tool_name in {"shell_exec", "background_process_start"} or isinstance(tool_instance, LocalCustomTool):
        return "execute"
    if tool_name in _WRITE_TIER_BUILTINS:
        return "write"
    if tool_name == "ast_grep" and arguments is not None:
        return "write" if arguments.get("mode") == "replace" else "read"
    # MCP server tools declare ``write`` (omp's contract): they mutate server
    # state but do not run arbitrary code, so a ``read_only`` MCP tool stays
    # ``read`` while every other MCP tool is ``write`` rather than the generic
    # ``execute`` unknown-tool default.
    if isinstance(tool_instance, McpTool):
        return "read" if is_read_tier(effects) else "write"
    return "read" if is_read_tier(effects) else "execute"
