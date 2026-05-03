from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from pathlib import Path

from ..tools.contracts import Tool, ToolCall, ToolDefinition
from ..tools.local_custom import LocalCustomTool
from .permission import OperationClass, PathScope

EXTERNAL_PATH_PRECHECK_KEYS: Mapping[str, tuple[str, ...]] = {
    "edit": ("path",),
    "glob": ("path",),
    "grep": ("path",),
    "list": ("path",),
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


def extract_shell_path_candidates(command: str) -> tuple[str, ...]:
    try:
        lexer = shlex.shlex(command, posix=False)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    candidates: list[str] = []
    for index, token in enumerate(tokens):
        value = _normalize_shell_path_token(token)
        if not value:
            continue
        if index == 0 and _looks_like_shell_executable(value):
            continue
        if _is_shell_explicit_output_path_candidate(tokens, index, value):
            candidates.append(value)
    return tuple(candidates)


def _is_shell_explicit_output_path_candidate(tokens: list[str], index: int, value: str) -> bool:
    if not _looks_like_shell_path_candidate(value):
        return False
    token = tokens[index].strip()
    if _has_shell_output_redirection(token):
        return True
    option, has_inline_value = _shell_option_name(token)
    output_options = {"--output", "--output-document", "--out", "--outfile"}
    if option in output_options:
        return True
    previous = tokens[index - 1].strip() if index > 0 else ""
    if _has_shell_output_redirection(previous):
        return True
    if previous in output_options or previous == "-o":
        return True
    if has_inline_value:
        return False
    return False


def _has_shell_output_redirection(token: str) -> bool:
    stripped = token.strip().lstrip("0123456789")
    return stripped.startswith(">")


def _shell_option_name(token: str) -> tuple[str | None, bool]:
    stripped = token.strip().strip("\"'`")
    if not stripped.startswith("-"):
        return None, False
    if "=" in stripped:
        option, _value = stripped.split("=", 1)
        return option, True
    return stripped, False


def _normalize_shell_path_token(token: str) -> str:
    value = token.strip().strip("\"'`")
    redirection_index = 0
    while redirection_index < len(value) and value[redirection_index].isdigit():
        redirection_index += 1
    if redirection_index < len(value) and value[redirection_index] in ("<", ">"):
        value = value[redirection_index:]
    value = value.lstrip("<>")
    if "=" in value:
        _, assignment_value = value.split("=", 1)
        assignment_value = assignment_value.strip().strip("\"'`")
        if _looks_like_shell_path_candidate(assignment_value):
            return assignment_value
    return value


def _looks_like_shell_path_candidate(value: str) -> bool:
    normalized = value
    while normalized.startswith("./") or normalized.startswith(".\\"):
        normalized = normalized[2:]
    if normalized.startswith(("~/", "../", "..\\", "/")):
        return True
    return len(normalized) >= 3 and normalized[1] == ":" and normalized[2] in ("\\", "/")


def _looks_like_shell_executable(value: str) -> bool:
    if value.startswith(("/", "~/")):
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in ("\\", "/")
