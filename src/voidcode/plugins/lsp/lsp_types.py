"""Plugin-local LSP contracts shared by the tool and its manager.

There are no core-wide LSP contracts to import: this module defines the small
part the LSP plugin needs: the document-state record, the request-error base
class, the request boundary the tool calls, and the plugin's LSP config carrying
validated server configuration.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voidcode.capabilities.workspace_queries import PositionEncoding
from voidcode.sdk.json_values import json_wire_object

from .contracts import LspServerConfigOverride

if TYPE_CHECKING:
    from voidcode.sdk.provider import ProviderAbortSignal
    from voidcode.sdk.services import PluginServices


type LspServerStatus = Literal["stopped", "starting", "running", "failed"]
type LspManagerMode = Literal["disabled", "managed"]


@dataclass(frozen=True, slots=True)
class LspDocumentState:
    version: int
    content_hash: str
    position_encoding: PositionEncoding


class LspRequestError(ValueError):
    """A failure reported by the plugin-owned LSP request capability."""


class LspResponse(Protocol):
    @property
    def response(self) -> dict[str, object]: ...

    @property
    def document_state(self) -> LspDocumentState | None: ...

    @property
    def position_encoding(self) -> PositionEncoding: ...


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


class RuntimeLspConfig(BaseModel):
    """Validate the plugin-owned configuration once at admission."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    enabled: bool = True
    servers: dict[str, LspServerConfigOverride] = Field(default_factory=dict)

    @field_validator("servers")
    @classmethod
    def _server_names(cls, servers: dict[str, LspServerConfigOverride]) -> dict[str, LspServerConfigOverride]:
        if any(not name.strip() for name in servers):
            raise ValueError("server names must not be empty")
        return servers


def lsp_config_from_mapping(config: Mapping[str, object] | None) -> RuntimeLspConfig:
    raw = {key: value for key, value in (config or {}).items() if key != "providers"}
    return RuntimeLspConfig.model_validate(json_wire_object(raw))
