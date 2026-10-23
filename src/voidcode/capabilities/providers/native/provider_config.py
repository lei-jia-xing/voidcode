from __future__ import annotations

from dataclasses import replace

from .config import (
    PROVIDER_WIRES,
    CopilotProviderConfig,
    GoogleProviderConfig,
    OpenAIProviderConfig,
    ProviderEndpointConfig,
    openai_compatible_default_base_url,
)
from .provider_table import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID

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

# Google's config names no base URL by default -- the SDK resolves its own -- so
# the listing derives from the Gemini API host the SDK would use.
_DEFAULT_GOOGLE_BASE_URL = next(row.default_base_url for row in PROVIDER_TABLE if row.wire == "google-generative-ai")

# The endpoint provider is a user-supplied OpenAI-compatible gateway. Without
# configuration it assumes the conventional local gateway.
DEFAULT_ENDPOINT_BASE_URL = PROVIDER_TABLE_BY_ID["endpoint"].default_base_url


def openai_wire_default_base_url(provider_name: str) -> str | None:
    """Vendor default base URL for an OpenAI-wire provider that names none.

    ``None`` means the provider has no default endpoint of its own, so a caller
    must refuse to run rather than point the wire at a host it does not own.
    """
    vendor_default = _DEFAULT_OPENAI_WIRE_BASE_URLS.get(provider_name)
    if vendor_default is not None:
        return vendor_default
    return openai_compatible_default_base_url(provider_name) or None


def openai_provider_config(config: OpenAIProviderConfig | None) -> ProviderEndpointConfig:
    configured_base_url = None if config is None else config.base_url
    return ProviderEndpointConfig(
        api_key=None if config is None else config.api_key,
        # OpenAI's own host, stated here instead of borrowed from the transport's
        # construction default.
        base_url=configured_base_url or _DEFAULT_OPENAI_WIRE_BASE_URLS["openai"],
        timeout_seconds=None if config is None else config.timeout_seconds,
        model_map={},
        openai_organization=None if config is None else config.organization,
        openai_project=None if config is None else config.project,
    )


def anthropic_wire_default_base_url(provider_name: str) -> str | None:
    """Vendor default base URL for an Anthropic-wire provider that names none.

    ``None`` means the provider has no default endpoint of its own, so a caller
    must refuse to run rather than point the wire at a host it does not own.
    """
    return _DEFAULT_ANTHROPIC_WIRE_BASE_URLS.get(provider_name)


def google_provider_config(config: GoogleProviderConfig | None) -> ProviderEndpointConfig:
    api_key = None
    auth_header = None
    auth_scheme = "bearer"
    auth = None if config is None else config.auth
    if auth is not None:
        if auth.method == "api_key":
            api_key = auth.api_key
            auth_header = "x-goog-api-key"
            auth_scheme = "token"
        elif auth.method == "oauth":
            api_key = auth.access_token
    configured_base_url = None if config is None else config.base_url
    # A service-account configuration targets Vertex rather than the Gemini API host.
    base_url = configured_base_url if auth is not None and auth.method == "service_account" else configured_base_url or _DEFAULT_GOOGLE_BASE_URL
    return ProviderEndpointConfig(
        api_key=api_key,
        base_url=base_url,
        auth_header=auth_header,
        auth_scheme=auth_scheme,
        timeout_seconds=None if config is None else config.timeout_seconds,
        model_map={},
        wire="google-generative-ai",
    )


def copilot_provider_config(config: CopilotProviderConfig | None) -> ProviderEndpointConfig:
    token = None if config is None or config.auth is None else config.auth.token
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
    api_key_env_var: str,
) -> ProviderEndpointConfig:
    """Normalize one gateway vendor's configuration against that vendor's own defaults.

    Unconfigured, the vendor keeps its own host. Configured, every field the
    caller set survives and only the endpoints it left out fall back to the
    vendor's. The model listing always derives from the resolved base URL.
    """
    if config is None:
        return ProviderEndpointConfig(
            base_url=base_url,
            api_key_env_var=api_key_env_var,
            model_map={},
        )
    return ProviderEndpointConfig(
        api_key=config.api_key,
        api_key_env_var=config.api_key_env_var,
        base_url=config.base_url or base_url,
        auth_header=config.auth_header,
        auth_scheme=config.auth_scheme,
        auth_scheme_explicit=config.auth_scheme_explicit,
        ssl_verify=config.ssl_verify,
        timeout_seconds=config.timeout_seconds,
        model_map=dict(config.model_map),
        transient_retry=config.transient_retry,
    )
