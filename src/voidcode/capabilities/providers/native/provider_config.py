from __future__ import annotations

from dataclasses import replace

from ..registry import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID
from .config import (
    PROVIDER_WIRES,
    CopilotProviderConfig,
    ProviderEndpointConfig,
)

# Vendor defaults for the providers whose wire is the OpenAI chat protocol, read
# from the provider table: OpenAI itself, Copilot, and every shared
# OpenAI-compatible vendor. The named endpoints (``endpoint``, ``opencode-zen``,
# ``openrouter``) are deliberately absent -- each resolves its own host -- so a
# config that names no ``base_url`` for one of them is rejected instead of
# guessed at.
_DEFAULT_OPENAI_WIRE_BASE_URLS: dict[str, str] = {
    provider_id: PROVIDER_TABLE_BY_ID[provider_id].default_base_url
    for provider_id, wire in PROVIDER_WIRES.items()
    if wire.shape in ("openai", "copilot", "openai_compatible")
}

# Vendor defaults for the providers whose wire is the Anthropic Messages
# protocol: exactly the table rows on that wire. Same contract as the OpenAI
# wire table above -- a provider absent from this table has no default of its
# own, so a caller must refuse to run rather than point the wire at a host it
# does not own. The Anthropic SDK transport in ``anthropic_native`` keeps its
# own construction default for direct use only.
_DEFAULT_ANTHROPIC_WIRE_BASE_URLS: dict[str, str] = {row.id: row.default_base_url for row in PROVIDER_TABLE if row.wire == "anthropic-messages"}


# The endpoint provider is a user-supplied OpenAI-compatible gateway. Without
# configuration it assumes the conventional local gateway.
DEFAULT_ENDPOINT_BASE_URL = PROVIDER_TABLE_BY_ID["endpoint"].default_base_url


def openai_wire_default_base_url(provider_name: str) -> str | None:
    """Vendor default base URL for an OpenAI-wire provider that names none.

    ``None`` means the provider has no default endpoint of its own, so a caller
    must refuse to run rather than point the wire at a host it does not own.
    """
    return _DEFAULT_OPENAI_WIRE_BASE_URLS.get(provider_name)


def anthropic_wire_default_base_url(provider_name: str) -> str | None:
    """Vendor default base URL for an Anthropic-wire provider that names none.

    ``None`` means the provider has no default endpoint of its own, so a caller
    must refuse to run rather than point the wire at a host it does not own.
    """
    return _DEFAULT_ANTHROPIC_WIRE_BASE_URLS.get(provider_name)


def copilot_provider_config(config: CopilotProviderConfig | None) -> ProviderEndpointConfig:
    auth = None if config is None else config.auth
    token = None if auth is None else auth.token
    configured_base_url = None if config is None else config.base_url
    return ProviderEndpointConfig(
        api_key=token,
        # Copilot calls its own host: a Copilot token must never be sent to
        # ``api.openai.com`` just because no base URL was configured.
        base_url=configured_base_url or _DEFAULT_OPENAI_WIRE_BASE_URLS["github-copilot"],
        timeout_seconds=None if config is None else config.timeout_seconds,
    )


def endpoint_provider_config(config: ProviderEndpointConfig | None) -> ProviderEndpointConfig:
    """Normalize the configuration-driven endpoint provider.

    An unconfigured endpoint keeps the local gateway default, so resolution
    never silently targets a first-party API.
    """
    if config is None:
        return ProviderEndpointConfig(base_url=DEFAULT_ENDPOINT_BASE_URL)
    return replace(
        config,
        base_url=config.base_url or DEFAULT_ENDPOINT_BASE_URL,
    )


def vendor_endpoint_config(
    config: ProviderEndpointConfig | None,
    *,
    base_url: str,
) -> ProviderEndpointConfig:
    """Normalize one gateway vendor's configuration against that vendor's own defaults.

    Unconfigured, the vendor keeps its own host. Configured, every field the
    caller set survives and only the endpoints it left out fall back to the
    vendor's.
    """
    if config is None:
        return ProviderEndpointConfig(base_url=base_url, model_map={})
    return replace(config, base_url=config.base_url or base_url)
