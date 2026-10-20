"""Plugin-local LSP contracts shared by the tool and its manager.

There are no kernel-wide LSP contracts to import: this module defines the small
part the LSP plugin needs: the document-state record, the request-error base
class, the request boundary the tool calls, and the plugin's LSP config carrying
the ``enabled`` tri-state.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .contracts import LspServerConfigOverride

if TYPE_CHECKING:
    from voidcode.sdk.provider import ProviderAbortSignal
    from voidcode.sdk.services import PluginServices


@dataclass(frozen=True, slots=True)
class LspDocumentState:
    version: int
    content_hash: str
    position_encoding: str


class LspRequestError(ValueError):
    """A failure reported by the plugin-owned LSP request capability."""


class LspResponse(Protocol):
    @property
    def response(self) -> dict[str, object]: ...

    @property
    def document_state(self) -> LspDocumentState | None: ...

    @property
    def position_encoding(self) -> str: ...


class LspRequester(Protocol):
    def __call__(
        self,
        *,
        server_name: str | None,
        method: str,
        params: dict[str, object],
        workspace: Path,
        services: PluginServices,
        session_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> LspResponse: ...


@dataclass(frozen=True, slots=True)
class RuntimeLspConfig:
    """The plugin's own LSP section: ``enabled`` plus per-server overrides."""

    enabled: bool | None = None
    servers: Mapping[str, LspServerConfigOverride] | None = None


def runtime_capability_enabled(enabled: bool | None) -> bool:
    """Resolve a capability ``enabled`` tri-state to a boolean.

    LSP tooling is enabled by default: ``None`` (unset) means enabled, and only
    an explicit ``enabled: false`` disables the capability. Server startup stays
    gated on a workspace actually resolving to a server, so a default-on plugin
    with no configured servers is a safe no-op that spawns no process.
    """
    return enabled is not False


def lsp_config_from_mapping(config: Mapping[str, object] | None) -> RuntimeLspConfig:
    """Build the plugin's LSP config from its untrusted config mapping."""
    if not isinstance(config, Mapping):
        return RuntimeLspConfig()
    raw_enabled = config.get("enabled")
    enabled = raw_enabled if isinstance(raw_enabled, bool) else None
    raw_servers = config.get("servers")
    if not isinstance(raw_servers, Mapping):
        return RuntimeLspConfig(enabled=enabled)
    servers = {name: _server_override(raw) for name, raw in raw_servers.items() if isinstance(name, str) and isinstance(raw, Mapping)}
    return RuntimeLspConfig(enabled=enabled, servers=servers or None)


def _server_override(raw: Mapping[str, object]) -> LspServerConfigOverride:
    return LspServerConfigOverride(
        preset=_optional_str(raw.get("preset")),
        command=_string_tuple(raw.get("command")),
        extensions=_string_tuple(raw.get("extensions")),
        languages=_string_tuple(raw.get("languages")),
        root_markers=_string_tuple(raw.get("root_markers")),
        settings=_plain_mapping(raw.get("settings")),
        init_options=_plain_mapping(raw.get("init_options")),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _plain_mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}
