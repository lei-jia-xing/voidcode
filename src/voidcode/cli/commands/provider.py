"""``voidcode provider``: provider model discovery and configuration inspection."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ...cli_support import EXIT_PROVIDER_ERROR, EXIT_SUCCESS
from ...runtime.provider_inspection import ProviderEndpointFacts, RuntimeProviderEndpointInspector
from ..handler_args import ProviderArgs
from ..options import workspace_option
from ..provider_view import provider_inspect_payload, provider_model_metadata_payload
from ..runtime_gateway import load_cli_config, open_runtime, runtime_error_boundary


def _handle_provider_models_command(args: ProviderArgs) -> int:
    workspace = args.workspace
    provider = args.provider
    assert provider is not None
    refresh = args.refresh
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        if refresh:
            _ = runtime.refresh_provider_models(provider)
        result = runtime.provider_models_result(provider)

    payload: dict[str, object] = {
        "workspace": str(workspace),
        "provider": provider,
        "refreshed": refresh,
        "models": list(result.models),
        "model_metadata": {model: provider_model_metadata_payload(metadata) for model, metadata in result.model_metadata.items()},
        "source": result.source,
        "last_refresh_status": result.last_refresh_status,
        "last_error": result.last_error,
        "discovery_mode": result.discovery_mode,
    }
    if refresh and result.source == "fallback":
        print(
            f"WARN provider.models.refresh provider={provider} source=fallback reason={result.last_error or 'remote discovery unavailable'}; "
            "use the configured model explicitly or check provider credentials.",
            file=sys.stderr,
            flush=True,
        )

    print(json.dumps(payload))
    if refresh and result.source == "fallback":
        return EXIT_PROVIDER_ERROR
    return EXIT_SUCCESS


def _provider_endpoint_facts_for_cli(workspace: Path, provider_name: str) -> ProviderEndpointFacts:
    """Resolve the inspected provider's endpoint from the runtime config the CLI would load."""
    config = load_cli_config(workspace)
    return RuntimeProviderEndpointInspector(providers=config.providers).facts(provider_name)


def _handle_provider_inspect_command(args: ProviderArgs) -> int:
    workspace = args.workspace
    provider = args.provider
    assert provider is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        result = runtime.inspect_provider(provider)

    endpoint = _provider_endpoint_facts_for_cli(workspace, provider)
    print(json.dumps(provider_inspect_payload(result, workspace=workspace, endpoint=endpoint), sort_keys=True))
    readiness = result.readiness
    if readiness is not None and not readiness.ok:
        return EXIT_PROVIDER_ERROR
    if not result.validation.ok:
        return EXIT_PROVIDER_ERROR
    return EXIT_SUCCESS


@click.group(help="Inspect provider metadata.")
def provider() -> None:
    pass


@provider.command(help="Show or refresh available models for one provider.")
@click.argument("provider_name")
@workspace_option("Workspace root used to resolve runtime config.")
@click.option("--refresh", is_flag=True)
def models(provider_name: str, workspace: Path, refresh: bool) -> int:
    return _handle_provider_models_command(
        ProviderArgs(
            provider=provider_name,
            workspace=workspace,
            refresh=refresh,
        )
    )


@provider.command(help="Show configured status, model limits, and model capabilities.")
@click.argument("provider_name")
@workspace_option("Workspace root used to resolve runtime config.")
def inspect(provider_name: str, workspace: Path) -> int:
    return _handle_provider_inspect_command(
        ProviderArgs(
            provider=provider_name,
            workspace=workspace,
        )
    )
