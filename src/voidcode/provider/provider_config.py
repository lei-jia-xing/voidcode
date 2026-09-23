from __future__ import annotations

from dataclasses import replace

from .config import (
    AnthropicProviderConfig,
    CopilotProviderConfig,
    GoogleProviderConfig,
    OpenAIProviderConfig,
    ProviderEndpointConfig,
    openai_compatible_default_base_url,
)

_DEFAULT_DISCOVERY_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
}

# Vendor defaults for the providers whose wire is the OpenAI chat protocol.
# A config that names no ``base_url`` resolves to the provider's own host here
# -- never to another vendor's -- and a provider absent from this table has no
# default at all: an unconfigured provider is rejected instead of guessed at.
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
# Copilot credentials must never reach ``api.openai.com``.
_DEFAULT_COPILOT_BASE_URL = "https://api.individual.githubcopilot.com"
_DEFAULT_OPENAI_WIRE_BASE_URLS: dict[str, str] = {
    "openai": _DEFAULT_OPENAI_BASE_URL,
    "copilot": _DEFAULT_COPILOT_BASE_URL,
}

# Vendor defaults for the providers whose wire is the Anthropic Messages
# protocol. Same contract as the OpenAI wire table above: a provider absent from
# this table has no default of its own, so a caller must refuse to run rather
# than point the wire at a host it does not own. The Anthropic SDK transport in
# ``anthropic_native`` keeps its own construction default for direct use only.
_DEFAULT_ANTHROPIC_WIRE_BASE_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    # Kimi's coding subscription API and MiniMax's China-region Anthropic
    # surface; hosts taken from pi's provider definitions.
    "kimi-coding": "https://api.kimi.com/coding",
    "minimax-cn": "https://api.minimaxi.com/anthropic",
}

# The endpoint provider is a user-supplied OpenAI-compatible gateway. Without
# configuration it assumes the conventional local gateway, mirroring the
# discovery default documented for `providers.endpoint`.
DEFAULT_ENDPOINT_BASE_URL = "http://127.0.0.1:4000/v1"
DEFAULT_ENDPOINT_DISCOVERY_URL = "http://127.0.0.1:4000"


def default_discovery_base_url(provider_name: str, *, configured_base_url: str | None, configured_discovery_base_url: str | None) -> str | None:
    if configured_discovery_base_url is not None:
        return configured_discovery_base_url
    if configured_base_url is not None:
        return None
    return _DEFAULT_DISCOVERY_BASE_URLS.get(provider_name)


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
        base_url=configured_base_url or _DEFAULT_OPENAI_BASE_URL,
        discovery_base_url=default_discovery_base_url(
            "openai",
            configured_base_url=None if config is None else config.base_url,
            configured_discovery_base_url=None if config is None else config.discovery_base_url,
        ),
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


def anthropic_compatible_endpoint_config(provider_name: str, config: AnthropicProviderConfig | None) -> ProviderEndpointConfig:
    """Normalize one Anthropic-wire vendor's configuration.

    The vendor default base URL and the discovery host both come from tables
    keyed by ``provider_name``, so a gateway route that pins its own host (the
    OpenCode adapters) keeps that host while an unconfigured vendor resolves to
    its own endpoint instead of borrowing Anthropic's.
    """
    vendor_default_base_url = anthropic_wire_default_base_url(provider_name)
    if vendor_default_base_url is None:
        raise ValueError(f"Unknown Anthropic-wire provider: {provider_name!r}")
    configured_base_url = None if config is None else config.base_url
    configured_discovery_base_url = None if config is None else config.discovery_base_url
    discovery_base_url = default_discovery_base_url(
        provider_name,
        configured_base_url=configured_base_url,
        configured_discovery_base_url=configured_discovery_base_url,
    )
    if discovery_base_url is None and configured_base_url is None:
        # Nothing configured and no verified listing host of its own: disable
        # discovery. ``None`` here would make the catalog probe
        # ``<vendor base>/models`` as an OpenAI listing, which nobody has
        # verified for these Anthropic-wire hosts. A configured
        # ``discovery_base_url`` or ``base_url`` still decides for itself above.
        discovery_base_url = ""
    return ProviderEndpointConfig(
        api_key=None if config is None else config.api_key,
        base_url=configured_base_url or vendor_default_base_url,
        discovery_base_url=discovery_base_url,
        timeout_seconds=None if config is None else config.timeout_seconds,
        model_map={},
        anthropic_messages_compatible=True,
        cache_retention="none" if config is None else config.cache_retention,
    )


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
    discovery_base_url = default_discovery_base_url(
        "google",
        configured_base_url=None if config is None else config.base_url,
        configured_discovery_base_url=None if config is None else config.discovery_base_url,
    )
    if auth is not None and auth.method == "service_account":
        # Service-account auth resolves no credential this discovery path can
        # send, so an unauthenticated probe must not be issued: report discovery
        # as disabled rather than pretending it is available.
        discovery_base_url = ""
    return ProviderEndpointConfig(
        api_key=api_key,
        base_url=None if config is None else config.base_url,
        discovery_base_url=discovery_base_url,
        auth_header=auth_header,
        auth_scheme=auth_scheme,
        timeout_seconds=None if config is None else config.timeout_seconds,
        model_map={},
    )


def copilot_provider_config(config: CopilotProviderConfig | None) -> ProviderEndpointConfig:
    token = None if config is None or config.auth is None else config.auth.token
    configured_base_url = None if config is None else config.base_url
    return ProviderEndpointConfig(
        api_key=token,
        # Copilot calls its own host: a Copilot token must never be sent to
        # ``api.openai.com`` just because no base URL was configured.
        base_url=configured_base_url or _DEFAULT_COPILOT_BASE_URL,
        # Copilot has no public model listing, so the default host disables
        # discovery instead of issuing an unauthenticated probe.
        discovery_base_url=None if configured_base_url else "",
        timeout_seconds=None if config is None else config.timeout_seconds,
    )


def endpoint_provider_config(config: ProviderEndpointConfig | None) -> ProviderEndpointConfig:
    """Normalize the configuration-driven endpoint provider.

    An unconfigured endpoint keeps the local gateway defaults, so resolution
    never silently targets a first-party API.
    """
    if config is None:
        return ProviderEndpointConfig(
            base_url=DEFAULT_ENDPOINT_BASE_URL,
            discovery_base_url=DEFAULT_ENDPOINT_DISCOVERY_URL,
        )
    return replace(
        config,
        base_url=config.base_url or DEFAULT_ENDPOINT_BASE_URL,
        discovery_base_url=config.discovery_base_url if config.discovery_base_url is not None else DEFAULT_ENDPOINT_DISCOVERY_URL,
    )


def vendor_endpoint_config(
    config: ProviderEndpointConfig | None,
    *,
    base_url: str,
    discovery_base_url: str,
    api_key_env_var: str,
) -> ProviderEndpointConfig:
    """Normalize one gateway vendor's configuration against that vendor's own defaults.

    Unconfigured, the vendor keeps its own host and its own model listing.
    Configured, every field the caller set survives and only the endpoints it left
    out fall back to the vendor's; a ``base_url`` with no ``discovery_base_url``
    disables discovery rather than guessing at a listing path the caller did not
    name.
    """
    if config is None:
        return ProviderEndpointConfig(
            base_url=base_url,
            discovery_base_url=discovery_base_url,
            api_key_env_var=api_key_env_var,
            model_map={},
        )
    return ProviderEndpointConfig(
        api_key=config.api_key,
        api_key_env_var=config.api_key_env_var,
        base_url=config.base_url or base_url,
        discovery_base_url=(
            config.discovery_base_url if config.discovery_base_url is not None else (None if config.base_url else discovery_base_url)
        ),
        auth_header=config.auth_header,
        auth_scheme=config.auth_scheme,
        auth_scheme_explicit=config.auth_scheme_explicit,
        ssl_verify=config.ssl_verify,
        timeout_seconds=config.timeout_seconds,
        model_map=dict(config.model_map),
        transient_retry=config.transient_retry,
    )
