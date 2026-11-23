"""LSP Tool: filesystem-nonmutating adapter over the plugin-owned LSP manager."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from lsprotocol import converters as lsp_converters
from lsprotocol import types as lsp_types
from pydantic import BaseModel, ConfigDict, Field

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.provider import ProviderAbortSignal
from voidcode.sdk.services import ArtifactServices, PluginServices
from voidcode.sdk.tools import (
    OpaqueToolBody,
    OutputBounds,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from .lsp_manager import LspManager, LspRequest
from .lsp_types import LspDocumentState, LspRequester, LspRequestError, LspResponse


@enum.unique
class LspOperation(enum.Enum):
    DIAGNOSTICS = "textDocument/diagnostic"
    GO_TO_DEFINITION = "textDocument/definition"
    FIND_REFERENCES = "textDocument/references"
    RENAME = "textDocument/rename"
    CODE_ACTION = "textDocument/codeAction"
    WILL_RENAME_FILES = "workspace/willRenameFiles"
    HOVER = "textDocument/hover"
    DOCUMENT_SYMBOL = "textDocument/documentSymbol"
    WORKSPACE_SYMBOL = "workspace/symbol"
    GO_TO_IMPLEMENTATION = "textDocument/implementation"
    PREPARE_CALL_HIERARCHY = "textDocument/prepareCallHierarchy"
    INCOMING_CALLS = "callHierarchy/incomingCalls"
    OUTGOING_CALLS = "callHierarchy/outgoingCalls"


type LspToolOperation = Literal[
    "codeAction",
    "diagnostics",
    "documentSymbol",
    "findReferences",
    "goToDefinition",
    "goToImplementation",
    "hover",
    "incomingCalls",
    "outgoingCalls",
    "rename",
    "willRenameFiles",
    "workspaceSymbol",
]
type WorkspaceEditKind = Literal["edit", "rename", "create", "delete"]

#: The one source for the accepted tool-operation names: schema enum and lookup both read it.
_LSP_OPERATIONS: dict[LspToolOperation, LspOperation] = {
    "codeAction": LspOperation.CODE_ACTION,
    "diagnostics": LspOperation.DIAGNOSTICS,
    "documentSymbol": LspOperation.DOCUMENT_SYMBOL,
    "findReferences": LspOperation.FIND_REFERENCES,
    "goToDefinition": LspOperation.GO_TO_DEFINITION,
    "goToImplementation": LspOperation.GO_TO_IMPLEMENTATION,
    "hover": LspOperation.HOVER,
    "incomingCalls": LspOperation.INCOMING_CALLS,
    "outgoingCalls": LspOperation.OUTGOING_CALLS,
    "rename": LspOperation.RENAME,
    "willRenameFiles": LspOperation.WILL_RENAME_FILES,
    "workspaceSymbol": LspOperation.WORKSPACE_SYMBOL,
}


def _bound_requester(manager: LspManager) -> LspRequester:
    """The tool's only view of the plugin-owned manager."""

    def request(
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
        services: PluginServices,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> LspResponse:
        return manager.request(
            LspRequest(
                server_name=server_name,
                method=method,
                params=params,
                workspace=workspace,
                session_id=session_id,
                services=services,
                abort_signal=abort_signal,
            )
        )

    return request


class _LspArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    operation: LspToolOperation
    path: str
    line: int | None = Field(default=None, ge=1)
    character: int | None = Field(default=None, ge=1)
    query: str | None = None
    server: str | None = None
    newName: str | None = Field(default=None, min_length=1)
    newPath: str | None = Field(default=None, min_length=1)
    endLine: int | None = Field(default=None, ge=1)
    endCharacter: int | None = Field(default=None, ge=1)
    diagnostics: list[dict[str, object]] = Field(default_factory=list)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=50)


class LspTool:
    """Filesystem-nonmutating adapter over the plugin-owned LSP manager; this class also owns the invocation schema/effects."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="lsp",
        icon=Icon(glyph="λ"),
        description="LSP client for basic code intelligence lookups.",
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(_LSP_OPERATIONS),
                    "description": (
                        "LSP operation. Preferred names include diagnostics, goToDefinition, findReferences, hover, documentSymbol, "
                        "workspaceSymbol, goToImplementation, incomingCalls, and outgoingCalls."
                    ),
                },
                "path": {"type": "string", "description": "Workspace-relative file path used for target selection and server resolution."},
                "line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based line number as shown in editors. Required for position-based operations.",
                },
                "character": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based character number as shown in editors. Required for position-based operations.",
                },
                "query": {"type": "string", "description": "Optional workspaceSymbol search query. Empty string requests all symbols."},
                "server": {"type": "string", "description": "Optional configured LSP server name."},
                "newName": {"type": "string", "minLength": 1, "description": "Replacement symbol name for rename."},
                "newPath": {"type": "string", "minLength": 1, "description": "Destination workspace-relative path for willRenameFiles."},
                "endLine": {"type": "integer", "minimum": 1, "description": "Optional 1-based end line for codeAction; defaults to line."},
                "endCharacter": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional 1-based end character for codeAction; defaults to character.",
                },
                "diagnostics": {"type": "array", "items": {"type": "object"}, "description": "Optional LSP diagnostics used as codeAction context."},
                "offset": {"type": "integer", "minimum": 1, "default": 1, "description": "1-based result page offset."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50, "description": "Maximum results to render per page."},
            },
            "required": ["operation", "path"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ, ToolEffect.EXECUTE}),
        path_argument_keys=("path",),
    )

    def __init__(self, manager: LspManager) -> None:
        self._converter = lsp_converters.get_converter()
        self._requester: LspRequester = _bound_requester(manager)

    @staticmethod
    def _invoke_requester(
        requester: LspRequester,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
        services: PluginServices,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> LspResponse:
        try:
            return requester(
                server_name=server_name,
                method=method,
                params=params,
                workspace=workspace,
                services=services,
                session_id=session_id,
                abort_signal=abort_signal,
            )
        except LspRequestError as exc:
            raise ValueError(f"LSP protocol error: {exc}") from exc

    def _request_failure(self, exc: ValueError) -> ToolFailure:
        return ToolFailure(tool_name=self.definition.name, error=str(exc))

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:

        workspace = context.require_workspace()
        requester = self._requester
        args = _LspArgs.model_validate(json_wire_object(call.arguments))
        file_path = args.path
        line_value, character_value = args.line, args.character
        query, server, new_name, new_path = args.query, args.server, args.newName, args.newPath
        offset_value, limit_value = args.offset, args.limit
        operation = _LSP_OPERATIONS[args.operation]
        position_required = operation not in (
            LspOperation.DIAGNOSTICS,
            LspOperation.DOCUMENT_SYMBOL,
            LspOperation.WORKSPACE_SYMBOL,
            LspOperation.WILL_RENAME_FILES,
        )
        if operation == LspOperation.RENAME and not isinstance(new_name, str):
            raise ValueError("lsp rename requires a string 'newName' argument")
        if operation == LspOperation.WILL_RENAME_FILES and not isinstance(new_path, str):
            raise ValueError("lsp willRenameFiles requires a string 'newPath' argument")
        if position_required and (line_value is None or character_value is None):
            raise ValueError("lsp requires numeric 'line' and 'character' arguments (1-based)")
        relative_path = Path(file_path)
        workspace_root = workspace.resolve()
        candidate = (workspace_root / relative_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            raise ValueError("lsp target must be inside the current workspace")
        if not candidate.is_file():
            raise ValueError(f"lsp target does not exist: {file_path}")

        position = (
            lsp_types.Position(line=line_value - 1, character=character_value - 1) if line_value is not None and character_value is not None else None
        )
        text_document = lsp_types.TextDocumentIdentifier(uri=candidate.as_uri())
        params: dict[str, object]
        if operation == LspOperation.WILL_RENAME_FILES:
            assert isinstance(new_path, str)
            destination = (workspace_root / new_path).resolve()
            if not destination.is_relative_to(workspace_root):
                raise ValueError("lsp newPath must be inside the current workspace")
            params = {"files": [{"oldUri": candidate.as_uri(), "newUri": destination.as_uri()}]}
        elif operation == LspOperation.DIAGNOSTICS:
            params = self._converter.unstructure(
                lsp_types.DocumentDiagnosticParams(text_document=text_document),
                unstructure_as=lsp_types.DocumentDiagnosticParams,
            )
        elif operation == LspOperation.DOCUMENT_SYMBOL:
            params = self._converter.unstructure(
                lsp_types.DocumentSymbolParams(text_document=text_document),
                unstructure_as=lsp_types.DocumentSymbolParams,
            )
        elif operation == LspOperation.RENAME and position is not None:
            assert isinstance(new_name, str)
            params = self._converter.unstructure(
                lsp_types.RenameParams(text_document=text_document, position=position, new_name=new_name),
                unstructure_as=lsp_types.RenameParams,
            )
        elif operation == LspOperation.CODE_ACTION and position is not None:
            end_line_value = args.endLine if args.endLine is not None else line_value
            end_character_value = args.endCharacter if args.endCharacter is not None else character_value
            if end_line_value is None or end_character_value is None or end_line_value < 1 or end_character_value < 1:
                raise ValueError("lsp codeAction endLine and endCharacter must be >= 1")
            params = self._converter.unstructure(
                lsp_types.CodeActionParams(
                    text_document=text_document,
                    range=lsp_types.Range(
                        start=position,
                        end=lsp_types.Position(line=end_line_value - 1, character=end_character_value - 1),
                    ),
                    context=lsp_types.CodeActionContext(
                        diagnostics=[self._converter.structure(item, lsp_types.Diagnostic) for item in args.diagnostics],
                    ),
                ),
                unstructure_as=lsp_types.CodeActionParams,
            )
        elif position is not None:
            params = self._converter.unstructure(
                lsp_types.TextDocumentPositionParams(text_document=text_document, position=position),
                unstructure_as=lsp_types.TextDocumentPositionParams,
            )
        else:
            params = {"textDocument": {"uri": candidate.as_uri()}}
        if operation == LspOperation.WORKSPACE_SYMBOL:
            params = self._converter.unstructure(
                lsp_types.WorkspaceSymbolParams(query=query or ""),
                unstructure_as=lsp_types.WorkspaceSymbolParams,
            )

        document_state = None
        response = None
        response_operation = operation
        if operation in (LspOperation.INCOMING_CALLS, LspOperation.OUTGOING_CALLS):
            assert position is not None
            try:
                prepare_result = self._invoke_requester(
                    requester,
                    server_name=server,
                    method=LspOperation.PREPARE_CALL_HIERARCHY.value,
                    params=self._converter.unstructure(
                        lsp_types.TextDocumentPositionParams(
                            text_document=text_document,
                            position=position,
                        ),
                        unstructure_as=lsp_types.TextDocumentPositionParams,
                    ),
                    workspace=workspace_root,
                    services=services,
                    session_id=context.require_session_id(),
                    abort_signal=context.abort_signal,
                )
            except ValueError as exc:
                return self._request_failure(exc)
            prepare_response = prepare_result.response
            prepare_error = prepare_response.get("error")
            if prepare_error is not None:
                raise ValueError(f"LSP prepareCallHierarchy error: {prepare_error}")

            prepare_payload = self._validated_result(prepare_result, LspOperation.PREPARE_CALL_HIERARCHY)
            if not prepare_payload:
                response = prepare_result
                response_operation = LspOperation.PREPARE_CALL_HIERARCHY
            else:
                assert isinstance(prepare_payload, list)
                params = {"item": prepare_payload[0]}
            document_state = prepare_result.document_state

        if response is None:
            try:
                response = self._invoke_requester(
                    requester,
                    server_name=server,
                    method=operation.value,
                    params=params,
                    workspace=workspace_root,
                    services=services,
                    session_id=context.require_session_id(),
                    abort_signal=context.abort_signal,
                )
            except ValueError as exc:
                return self._request_failure(exc)
        error_value = response.response.get("error")
        if error_value is not None:
            raise ValueError(f"LSP error: {error_value}")
        return self._success_result(
            response,
            services=services,
            operation=operation,
            file_path=file_path,
            workspace_root=workspace_root,
            line=line_value,
            character=character_value,
            new_name=new_name if isinstance(new_name, str) else None,
            query=query,
            offset=offset_value,
            limit=limit_value,
            document_state=document_state,
            response_operation=response_operation,
        )

    def _validated_result(self, response: LspResponse, operation: LspOperation) -> object:
        try:
            response_type = lsp_types.METHOD_TO_TYPES[operation.value][1]
            assert response_type is not None
            if (
                operation
                in (
                    LspOperation.DOCUMENT_SYMBOL,
                    LspOperation.WORKSPACE_SYMBOL,
                    LspOperation.FIND_REFERENCES,
                    LspOperation.CODE_ACTION,
                    LspOperation.PREPARE_CALL_HIERARCHY,
                    LspOperation.INCOMING_CALLS,
                    LspOperation.OUTGOING_CALLS,
                )
                and response.response.get("result") is not None
                and not isinstance(response.response["result"], list)
            ):
                raise ValueError("expected an array result")
            validated = self._converter.structure(response.response, response_type)
        except (ExceptionGroup, TypeError, ValueError) as exc:
            raise ValueError(f"LSP {operation.value} returned an invalid result") from exc
        return self._converter.unstructure(validated.result)

    def _success_result(
        self,
        response: LspResponse,
        *,
        services: ArtifactServices,
        operation: LspOperation,
        file_path: str,
        workspace_root: Path,
        line: int | None,
        character: int | None,
        new_name: str | None,
        query: str | None,
        offset: int,
        limit: int,
        document_state: LspDocumentState | None = None,
        response_operation: LspOperation | None = None,
    ) -> ToolSuccess:
        source_operation = response_operation or operation
        payload = self._validated_result(response, source_operation)
        rendered_text, item_count, is_truncated = _render_lsp_output(
            operation,
            payload,
            file_path=file_path,
            workspace_root=workspace_root,
            line=line,
            character=character,
            new_name=new_name,
            query=query,
            offset=offset,
            limit=limit,
        )
        document_state_payload, document_summary = _document_state_result(
            response,
            document_state=document_state,
        )
        response_artifact = services.spill_artifact(json.dumps(response.response, ensure_ascii=False, indent=2), media_type="application/json")
        artifact_uri = response_artifact.uri
        next_offset = offset + limit if is_truncated else None
        if item_count and offset <= item_count:
            page_summary = f"Items {offset}-{min(offset + limit - 1, item_count)} of {item_count}."
        elif item_count is None:
            page_summary = "The server did not include an item list in this response."
        else:
            page_summary = f"No items on page offset {offset}; total item count {item_count}."
        output_text = f"Full LSP JSON response artifact: {artifact_uri}.\n{page_summary}"
        if next_offset is not None:
            output_text += f" More results available; next_offset={next_offset}."
        output_text += f"\n{document_summary}\n\n{rendered_text}"
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(
                output_text,
                bounds=OutputBounds(
                    truncated=is_truncated,
                    partial=is_truncated,
                    reference=response_artifact,
                    source=f"lsp:{source_operation.value}",
                ),
            ),
            body=OpaqueToolBody(
                {
                    "operation": operation.value,
                    "path": file_path,
                    "position_encoding": response.position_encoding,
                    "document_state": document_state_payload,
                    "item_count": item_count,
                    "offset": offset,
                    "limit": limit,
                    "next_offset": next_offset,
                    "truncated": is_truncated,
                }
            ),
        )

    def __repr__(self) -> str:
        return "<LspTool plugin-owned>"


def _document_state_result(
    response: LspResponse,
    *,
    document_state: LspDocumentState | None = None,
) -> tuple[dict[str, object] | None, str]:
    state = document_state or response.document_state
    if state is None:
        return None, f"Server position encoding: {response.position_encoding}; document synchronization unavailable."
    return (
        {
            "version": state.version,
            "content_hash": state.content_hash,
            "position_encoding": state.position_encoding,
        },
        f"Opened document version {state.version} (position encoding {state.position_encoding}; SHA-256 {state.content_hash}).",
    )


def _uri_to_rel_path(uri: str, workspace_root: Path) -> str:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        try:
            file_path = Path(url2pathname(unquote(parsed.path)))
            return file_path.relative_to(workspace_root).as_posix()
        except ValueError, OSError:
            return unquote(parsed.path)
    return uri


@dataclass(frozen=True, slots=True)
class _WorkspaceEditItem:
    kind: WorkspaceEditKind
    path: str
    version_label: str | None = None
    edit: dict[str, object] | None = None
    target_path: str | None = None


def _workspace_edit_items(payload: dict[str, object], workspace_root: Path) -> tuple[_WorkspaceEditItem, ...]:
    """Flatten text edits and file-operation edits into one ordered preview list."""
    items: list[_WorkspaceEditItem] = []
    changes = payload.get("changes")
    if isinstance(changes, dict) and changes:
        for uri, edits in changes.items():
            if not isinstance(uri, str) or not isinstance(edits, list):
                continue
            relative_path = _uri_to_rel_path(uri, workspace_root)
            items.extend(_WorkspaceEditItem("edit", relative_path, None, edit) for edit in edits if isinstance(edit, dict))
        return tuple(items)

    document_changes = payload.get("documentChanges")
    if not isinstance(document_changes, list):
        return ()
    for document in document_changes:
        if not isinstance(document, dict):
            continue
        kind = document.get("kind")
        if kind == "rename":
            old_uri = document.get("oldUri")
            new_uri = document.get("newUri")
            if isinstance(old_uri, str) and isinstance(new_uri, str):
                items.append(
                    _WorkspaceEditItem(
                        "rename",
                        _uri_to_rel_path(old_uri, workspace_root),
                        target_path=_uri_to_rel_path(new_uri, workspace_root),
                    )
                )
            continue
        if kind == "create" or kind == "delete":
            uri = document.get("uri")
            if isinstance(uri, str):
                items.append(_WorkspaceEditItem("create" if kind == "create" else "delete", _uri_to_rel_path(uri, workspace_root)))
            continue
        text_document = document.get("textDocument", {})
        if not isinstance(text_document, dict):
            continue
        uri = text_document.get("uri")
        if not isinstance(uri, str):
            continue
        version = text_document.get("version")
        version_label = f" (document version {version})" if isinstance(version, int) and not isinstance(version, bool) else None
        edits = document.get("edits", [])
        if not isinstance(edits, list):
            continue
        relative_path = _uri_to_rel_path(uri, workspace_root)
        items.extend(_WorkspaceEditItem("edit", relative_path, version_label, edit) for edit in edits if isinstance(edit, dict))
    return tuple(items)


def _workspace_edit_paths(payload: dict[str, object], workspace_root: Path) -> tuple[str, ...]:
    """Every path a workspace edit names, including resource-operation targets."""
    paths: list[str] = []
    for item in _workspace_edit_items(payload, workspace_root):
        paths.append(item.path)
        if item.target_path is not None:
            paths.append(item.target_path)
    return tuple(dict.fromkeys(paths))


_SYMBOL_KIND_NAMES = {kind.value: kind.name for kind in lsp_types.SymbolKind}

_SEVERITY_NAMES: dict[int, str] = {
    1: "error",
    2: "warning",
    3: "info",
    4: "hint",
}


def _render_lsp_output(
    operation: LspOperation,
    payload: object,
    *,
    file_path: str,
    workspace_root: Path,
    line: int | None = None,
    character: int | None = None,
    new_name: str | None = None,
    query: str | None = None,
    offset: int = 1,
    limit: int = 50,
) -> tuple[str, int | None, bool]:
    """Render one bounded LSP result page and return its total item count."""
    start = offset - 1

    if operation == LspOperation.DIAGNOSTICS:
        if isinstance(payload, dict):
            if payload.get("kind") == "unchanged":
                return f"Diagnostics unchanged for {file_path}; the full item list was not included.", None, False
            raw_items = payload.get("items")
            items = raw_items if isinstance(raw_items, list) else []
        else:
            items = payload if isinstance(payload, list) else []
        count = len(items)
        if not count:
            return f"No diagnostics reported for {file_path}.", 0, False
        page = items[start : start + limit]
        if not page:
            return f"No diagnostics at offset {offset}; {count} total.", count, False
        lines = [f"Diagnostics for {file_path} ({count} issue{'s' if count != 1 else ''}):"]
        for diag in page:
            if not isinstance(diag, dict):
                continue
            rng = diag.get("range", {})
            point = rng.get("start", {}) if isinstance(rng, dict) else {}
            s_line = point.get("line", 0) + 1
            s_char = point.get("character", 0) + 1
            severity = _SEVERITY_NAMES.get(diag.get("severity", 1), "error")
            source = diag.get("source")
            code = diag.get("code")
            extra = f" ({source} {code})" if source or code else ""
            lines.append(f"  [{severity.upper()}] {s_line}:{s_char} - {diag.get('message', '')}{extra}")
        return "\n".join(lines), count, start + len(page) < count

    if operation in (LspOperation.GO_TO_DEFINITION, LspOperation.GO_TO_IMPLEMENTATION, LspOperation.FIND_REFERENCES):
        label = {
            LspOperation.GO_TO_DEFINITION: "definition",
            LspOperation.GO_TO_IMPLEMENTATION: "implementation",
            LspOperation.FIND_REFERENCES: "reference",
        }[operation]
        locations = payload if isinstance(payload, list) else ([payload] if payload else [])
        count = len(locations)
        if not count:
            return f"No {label} found for {file_path} at {line}:{character}.", 0, False
        page = locations[start : start + limit]
        if not page:
            return f"No {label} locations at offset {offset}; {count} total.", count, False
        lines = [f"{label.capitalize()} location(s) for {file_path}:{line}:{character}:"]
        for location in page:
            if not isinstance(location, dict):
                continue
            uri = location.get("uri") or location.get("targetUri", "")
            relative_path = _uri_to_rel_path(uri, workspace_root)
            rng = location.get("range") or location.get("targetSelectionRange") or location.get("targetRange", {})
            start_point = rng.get("start", {}) if isinstance(rng, dict) else {}
            end_point = rng.get("end", {}) if isinstance(rng, dict) else {}
            s_line = start_point.get("line", 0) + 1
            s_character = start_point.get("character", 0) + 1
            e_line = end_point.get("line", 0) + 1
            e_character = end_point.get("character", 0) + 1
            lines.append(f"  {relative_path}:{s_line}:{s_character}-{e_line}:{e_character}")
        return "\n".join(lines), count, start + len(page) < count

    if operation == LspOperation.HOVER:
        if not isinstance(payload, dict):
            return f"No hover information available for {file_path} at {line}:{character}.", 0, False
        contents = payload.get("contents")
        if not contents:
            return f"No hover information available for {file_path} at {line}:{character}.", 0, False
        if isinstance(contents, dict):
            text = str(contents.get("value", ""))
        elif isinstance(contents, list):
            text = "\n".join(str(item.get("value", item) if isinstance(item, dict) else item) for item in contents)
        else:
            text = str(contents)
        text = text.strip()
        if not text:
            return f"No hover information available for {file_path} at {line}:{character}.", 0, False
        return f"Hover information for {file_path}:{line}:{character}:\n{text}", 1, False

    if operation == LspOperation.DOCUMENT_SYMBOL:
        symbols = payload if isinstance(payload, list) else []
        count = len(symbols)
        if not count:
            return f"No document symbols found in {file_path}.", 0, False
        page = symbols[start : start + limit]
        if not page:
            return f"No document symbols at offset {offset}; {count} total.", count, False
        lines = [f"Document symbols in {file_path}:"]

        def format_symbol(symbol: dict[str, object], depth: int) -> None:
            name = symbol.get("name", "<unnamed>")
            kind = _SYMBOL_KIND_NAMES.get(symbol.get("kind", 0), "Symbol")
            range_value = symbol.get("range")
            if not isinstance(range_value, dict):
                location = symbol.get("location")
                range_value = location.get("range", {}) if isinstance(location, dict) else {}
            rng = range_value
            start_point = rng.get("start", {}) if isinstance(rng, dict) else {}
            end_point = rng.get("end", {}) if isinstance(rng, dict) else {}
            s_line = start_point.get("line", 0) + 1
            e_line = end_point.get("line", 0) + 1
            lines.append(f"{'  ' * depth}- {name} ({kind}) [lines {s_line}-{e_line}]")
            children = symbol.get("children", [])
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        format_symbol(child, depth + 1)

        for symbol in page:
            if isinstance(symbol, dict):
                format_symbol(symbol, 1)
        return "\n".join(lines), count, start + len(page) < count

    if operation == LspOperation.WORKSPACE_SYMBOL:
        symbols = payload if isinstance(payload, list) else []
        query_label = f" matching '{query}'" if query else ""
        count = len(symbols)
        if not count:
            return f"No workspace symbols found{query_label}.", 0, False
        page = symbols[start : start + limit]
        if not page:
            return f"No workspace symbols at offset {offset}; {count} total.", count, False
        lines = [f"Workspace symbols{query_label} ({count} found):"]
        for symbol in page:
            if not isinstance(symbol, dict):
                continue
            name = symbol.get("name", "<unnamed>")
            kind = _SYMBOL_KIND_NAMES.get(symbol.get("kind", 0), "Symbol")
            location = symbol.get("location", {})
            uri = location.get("uri", "") if isinstance(location, dict) else ""
            relative_path = _uri_to_rel_path(uri, workspace_root)
            point = location.get("range", {}).get("start", {}) if isinstance(location, dict) else {}
            lines.append(f"  - {name} ({kind}) in {relative_path}:{point.get('line', 0) + 1}")
        return "\n".join(lines), count, start + len(page) < count

    if operation in (LspOperation.RENAME, LspOperation.WILL_RENAME_FILES):
        if not isinstance(payload, dict):
            message = (
                f"Rename to '{new_name}' produced no changes." if operation == LspOperation.RENAME else "No workspace edits needed for file rename."
            )
            return message, 0, False
        raw_edits = _workspace_edit_items(payload, workspace_root)
        count = len(raw_edits)
        if not count:
            message = (
                f"Rename to '{new_name}' produced no changes." if operation == LspOperation.RENAME else "No workspace edits needed for file rename."
            )
            return message, 0, False
        page = raw_edits[start : start + limit]
        if not page:
            return f"No workspace edits at offset {offset}; {count} total.", count, False
        title = (
            f"Rename preview for '{new_name}' (PREVIEW ONLY — no files modified):"
            if operation == LspOperation.RENAME
            else "willRenameFiles workspace edit preview:"
        )
        lines = [title]
        current_document: str | None = None
        for item in page:
            if item.kind != "edit":
                current_document = None
                if item.kind == "rename":
                    lines.append(f"  RENAME {item.path} -> {item.target_path or '<unchanged>'}")
                else:
                    lines.append(f"  {item.kind.upper()} {item.path}")
                continue
            edit = item.edit
            if edit is None:
                continue
            document_label = f"{item.path}{item.version_label or ''}"
            if document_label != current_document:
                lines.append(f"  {document_label}:")
                current_document = document_label
            rng = edit.get("range", {})
            start_point = rng.get("start", {}) if isinstance(rng, dict) else {}
            end_point = rng.get("end", {}) if isinstance(rng, dict) else {}
            s_line = start_point.get("line", 0) + 1
            s_character = start_point.get("character", 0) + 1
            e_line = end_point.get("line", 0) + 1
            e_character = end_point.get("character", 0) + 1
            lines.append(f"    line {s_line}:{s_character}-{e_line}:{e_character} -> {edit.get('newText', '')}")
        if operation == LspOperation.RENAME:
            lines.append("\nNote: Preview only. Apply with an available workspace-edit tool.")
        return "\n".join(lines), count, start + len(page) < count

    if operation == LspOperation.CODE_ACTION:
        actions = payload if isinstance(payload, list) else []
        count = len(actions)
        if not count:
            return f"No code actions available at {file_path}:{line}:{character}.", 0, False
        page = actions[start : start + limit]
        if not page:
            return f"No code actions at offset {offset}; {count} total.", count, False
        lines = [f"Available code actions at {file_path}:{line}:{character} (PREVIEW ONLY):"]
        for index, action in enumerate(page, start=offset):
            if not isinstance(action, dict):
                continue
            title = action.get("title", f"Action {index}")
            kind = action.get("kind", "refactor")
            lines.append(f"  {index}. [{kind}] {title}")
            edit = action.get("edit")
            if isinstance(edit, dict):
                paths = _workspace_edit_paths(edit, workspace_root)
                if paths:
                    lines.append(f"     affects: {', '.join(paths)}")
        lines.append("\nNote: Preview only. Apply with an available workspace-edit tool.")
        return "\n".join(lines), count, start + len(page) < count

    if operation in (LspOperation.INCOMING_CALLS, LspOperation.OUTGOING_CALLS):
        calls = payload if isinstance(payload, list) else []
        count = len(calls)
        if not count:
            return f"No call hierarchy results for {file_path}:{line}:{character}.", 0, False
        page = calls[start : start + limit]
        if not page:
            return f"No call hierarchy results at offset {offset}; {count} total.", count, False
        direction = "Incoming" if operation == LspOperation.INCOMING_CALLS else "Outgoing"
        lines = [f"{direction} calls for {file_path}:{line}:{character}:"]
        for call in page:
            if not isinstance(call, dict):
                continue
            item = call.get("from" if operation == LspOperation.INCOMING_CALLS else "to", {})
            name = item.get("name", "<unnamed>") if isinstance(item, dict) else ""
            uri = item.get("uri", "") if isinstance(item, dict) else ""
            lines.append(f"  - {name} in {_uri_to_rel_path(uri, workspace_root)}")
        return "\n".join(lines), count, start + len(page) < count

    return f"LSP {operation.value} response received.", 1, False
