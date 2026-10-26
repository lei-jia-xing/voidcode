"""The official ``lsp`` plugin: one language-server tool plus one document query.

Owns the LSP connection manager (session-scoped servers over the host's
supervised service processes, document state, request timeouts, shutdown) and
the ``lsp`` tool that renders bounded pages of 13 language-server operations.
Servers are resolved from the plugin config, or derived from workspace markers
when the config declares none, and are spawned lazily on the first request.
The ``lsp`` command reports the same manager's servers without spawning one,
and the ``workspace.document_version`` query answers the caller's session's
already-opened documents without starting a server or sending didOpen/didChange.
"""

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
