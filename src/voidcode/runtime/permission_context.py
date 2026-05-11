from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from ..security.shell_policy import extract_shell_path_candidates
from ..tools.contracts import Tool, ToolCall, ToolDefinition
from ..tools.local_custom import LocalCustomTool
from .permission import OperationClass, PathScope

EXTERNAL_PATH_PRECHECK_KEYS: Mapping[str, tuple[str, ...]] = {
    "edit": ("path",),
    "glob": ("path",),
    "grep": ("path",),
    "multi_edit": ("path", "filePath"),
    "read_file": ("filePath", "path"),
    "write_file": ("path",),
}


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
            tool.read_only,
            tool_instance=tool_instance,
        )
        candidate_paths = ()
        if tool_call.tool_name != "shell_exec":
            candidate_paths = self.candidate_paths_for_tool_call(
                tool_call,
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
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for external_path in external_paths:
            normalized.append(Path(external_path).as_posix())
        for raw_path in self.candidate_paths_for_tool_call(
            tool_call,
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
        patch_path_extractor: Callable[[str], tuple[str, ...]],
    ) -> tuple[str, ...]:
        arguments = tool_call.arguments
        candidates: list[str] = []
        for key in EXTERNAL_PATH_PRECHECK_KEYS.get(tool_call.tool_name, ()):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
        if tool_call.tool_name == "apply_patch":
            patch_text = arguments.get("patch")
            if isinstance(patch_text, str) and patch_text:
                candidates.extend(patch_path_extractor(patch_text))
        if tool_call.tool_name == "shell_exec":
            command = arguments.get("command")
            if isinstance(command, str):
                candidates.extend(extract_shell_path_candidates(command))
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
    read_only: bool,
    *,
    tool_instance: Tool,
) -> OperationClass:
    if tool_name == "shell_exec" or isinstance(tool_instance, LocalCustomTool):
        return "execute"
    return "read" if read_only else "write"
