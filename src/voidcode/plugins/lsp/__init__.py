"""LSP plugin: language-server tool and document state query."""

from collections.abc import Mapping
from pathlib import Path

from voidcode.capabilities.workspace_queries import DOCUMENT_STATES, DocumentState, DocumentStateRequest
from voidcode.sdk.plugin import Plugin, PluginQuery, PluginQueryCaller

from .inventory import LspInventoryCommand
from .lsp_manager import LspManager, build_lsp_manager
from .lsp_tool import LspTool
from .lsp_types import lsp_config_from_mapping


def _document_version_query(manager: LspManager) -> PluginQuery:
    def handler(request: DocumentStateRequest, caller: PluginQueryCaller) -> DocumentState | None:
        requested = Path(request.path)
        candidate = (requested if requested.is_absolute() else caller.workspace / requested).resolve()
        state = manager.document_state(session_id=caller.session_id, file_path=candidate, workspace=caller.workspace)
        if state is None:
            return None
        return DocumentState(
            path=candidate.as_posix(), version=state.version, content_hash=state.content_hash, position_encoding=state.position_encoding
        )

    return DOCUMENT_STATES.bind(handler)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    manager = build_lsp_manager(lsp_config_from_mapping(config))

    return Plugin(
        tools=(LspTool(manager),),
        commands=(LspInventoryCommand(manager),),
        queries=(_document_version_query(manager),),
        close=manager.shutdown,
    )
