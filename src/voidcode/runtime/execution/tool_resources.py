from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from ...core.tool_context import LspRequester, LspResponse, McpCallResult, McpRequester, RuleReader, ToolCommandHandler, ToolContext
from ...tools.contracts import RuntimeToolTimeoutError, ToolCall, ToolDefinition, ToolResult


@dataclass(frozen=True, slots=True)
class ToolCatalogReader:
    resolve: Callable[[str], ToolDefinition | None]

    def lookup(self, tool_name: str) -> ToolDefinition | None:
        return self.resolve(tool_name)


@dataclass(frozen=True, slots=True)
class SessionArtifactReader:
    caller_session_id: str
    read: Callable[..., dict[str, object]]

    def read_artifact(
        self,
        *,
        caller_session_id: str,
        artifact_id: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        if caller_session_id != self.caller_session_id:
            raise RuntimeError("artifact reader is bound to its invoking session")
        result = self.read(
            session_id=self.caller_session_id,
            artifact_id=artifact_id,
            offset=0 if offset is None else offset,
            limit=2000 if limit is None else limit,
        )
        return None if result.get("status") == "artifact_not_found" else result


@dataclass(frozen=True, slots=True)
class SessionTranscriptReader:
    caller_session_id: str
    read: Callable[..., dict[str, object] | None]

    def read_transcript(
        self,
        *,
        caller_session_id: str,
        session_id: str,
        limit: int | None = None,
    ) -> dict[str, object] | None:
        if caller_session_id != self.caller_session_id:
            raise RuntimeError("transcript reader is bound to its invoking session")
        return self.read(caller_session_id=self.caller_session_id, session_id=session_id, limit=limit)


@dataclass(frozen=True, slots=True)
class WorkspaceDiagnostics:
    workspace: Path
    request: Callable[..., dict[str, object]]

    def request_diagnostics(self, *, file_path: str, workspace: str) -> dict[str, object]:
        if Path(workspace) != self.workspace:
            raise RuntimeError("diagnostics reader is bound to its invoking workspace")
        return self.request(file_path=file_path, workspace=str(self.workspace))


def bind_tool_command(
    handler: ToolCommandHandler,
    *,
    call: ToolCall,
    context: ToolContext,
) -> ToolCommandHandler:
    approved_call = deepcopy(call)
    caller_session_id = context.require_session_id()

    def invoke(call: ToolCall, *, context: ToolContext) -> ToolResult:
        if call != approved_call:
            raise RuntimeError("runtime command is bound to its approved tool call")
        if (context.session_id, context.run_id, context.invocation_id) != (
            caller_session_id,
            authenticated_context.run_id,
            authenticated_context.invocation_id,
        ):
            raise RuntimeError("runtime command is bound to its invoking identity")
        if authenticated_context.abort_signal is not None and authenticated_context.abort_signal.cancelled:
            raise RuntimeToolTimeoutError(
                "runtime command cancelled before dispatch",
                cancellation_signalled=True,
            )
        return handler(approved_call, context=authenticated_context)

    authenticated_context = context
    return invoke


def bind_lsp_request(request: LspRequester, *, workspace: Path) -> LspRequester:
    def invoke(*, server_name: str | None, method: str, params: dict[str, object], workspace: Path) -> LspResponse:
        if workspace != authenticated_workspace:
            raise RuntimeError("LSP requester is bound to its invoking workspace")
        return request(server_name=server_name, method=method, params=params, workspace=authenticated_workspace)

    authenticated_workspace = workspace
    return invoke


def bind_mcp_request(request: McpRequester, *, call: ToolCall, workspace: Path) -> McpRequester:
    approved_arguments = deepcopy(call.arguments)

    def invoke(*, server_name: str, tool_name: str, arguments: dict[str, object], workspace: Path) -> McpCallResult:
        if f"mcp/{server_name}/{tool_name}" != call.tool_name or arguments != approved_arguments or workspace != authenticated_workspace:
            raise RuntimeError("MCP requester is bound to its approved tool call and workspace")
        return request(server_name=server_name, tool_name=tool_name, arguments=approved_arguments, workspace=authenticated_workspace)

    authenticated_workspace = workspace
    return invoke


def bind_rule_reader(read: RuleReader, *, workspace: Path) -> RuleReader:
    def invoke(path: str, *, workspace: Path, offset: int, limit: int) -> dict[str, object]:
        if workspace != authenticated_workspace:
            raise RuntimeError("rule reader is bound to its invoking workspace")
        return read(path, workspace=authenticated_workspace, offset=offset, limit=limit)

    authenticated_workspace = workspace
    return invoke
