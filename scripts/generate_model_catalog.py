"""Generate the bundled model catalog from the official models.dev API.

Dev-time generator only; never imported by the runtime. Fetches the
official JSON endpoint at https://models.dev/api.json
and emits `src/voidcode/capabilities/providers/native/model_catalog_data.json`, which
is the providers plugin's only static metadata source.

Field names in the emitted per-model entries must EXACTLY match
`ProviderModelMetadata` (the loader constructs the dataclass from them).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Final

import httpx

from voidcode.capabilities.providers.native.api_routes import api_route_for
from voidcode.capabilities.providers.native.provider_table import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID
from voidcode.capabilities.providers.native.reasoning_effort import CANONICAL_EFFORTS

CATALOG_URL = "https://models.dev/api.json"
_PROVIDER_PLUGIN = Path(__file__).resolve().parent.parent / "src" / "voidcode" / "capabilities" / "providers" / "native"
OUTPUT_PATH = _PROVIDER_PLUGIN / "model_catalog_data.json"
# Canonical provider id -> the upstream models.dev keys its catalog is built
# from, straight from the one provider table. A provider with no upstream key
# (a runtime-only gateway) contributes no catalog entry.
PROVIDER_KEYS: dict[str, tuple[str, ...]] = {row.id: row.models_dev_keys for row in PROVIDER_TABLE if row.models_dev_keys}


def _fetch_raw() -> bytes:
    response = httpx.get(
        CATALOG_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "voidcode-model-catalog-generator",
        },
        follow_redirects=True,
        timeout=30,
    )
    response.raise_for_status()
    return response.content


def _per_token(cost: dict[str, object], key: str) -> float | None:
    value = cost.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) or value < 0:
        return None
    return float(value) / 1_000_000


def _reasoning_effort_fields(raw: dict[str, object]) -> dict[str, object]:
    options = raw.get("reasoning_options")
    if not isinstance(options, list):
        return {}
    has_effort = any(isinstance(option, dict) and option.get("type") == "effort" for option in options)
    has_budget = any(isinstance(option, dict) and option.get("type") == "budget_tokens" for option in options)
    fields: dict[str, object] = {"supports_reasoning_effort": has_effort}
    if has_budget:
        fields["supports_thinking_budget"] = True
    values = next(
        (option.get("values") for option in options if isinstance(option, dict) and option.get("type") == "effort"),
        None,
    )
    if isinstance(values, list):
        levels = [level for level in CANONICAL_EFFORTS if level in values]
        if levels:
            fields["supported_effort_levels"] = levels
    return fields


#: Upstream ``provider.npm`` package -> the wire that SDK speaks. OMP consults
#: this for the OpenCode gateways only (``openai-compat.ts:6592-6605``), and it is
#: the only upstream signal for the wires a route pin does not cover.
_NPM_API_HINTS: Final[Mapping[str, str]] = {
    "@ai-sdk/openai": "openai-responses",
    "@ai-sdk/anthropic": "anthropic-messages",
    "@ai-sdk/google": "google-generative-ai",
}


def _model_api(provider_id: str, model_id: str, raw: dict[str, object]) -> str:
    """The wire one model speaks: route pin, then npm hint, then the provider's own wire.

    Same precedence as OMP's ``createOpenCodeApiResolution`` -- the per-id route
    pins win over upstream metadata, which wins over the provider default. The
    runtime consumes the emitted value verbatim and never recomputes it.
    """
    pinned = api_route_for(provider_id, model_id)
    if pinned is not None:
        return pinned.api
    provider = raw.get("provider")
    npm = provider.get("npm") if isinstance(provider, dict) else None
    if isinstance(npm, str) and npm in _NPM_API_HINTS:
        return _NPM_API_HINTS[npm]
    return PROVIDER_TABLE_BY_ID[provider_id].wire


def _model_entry(raw: dict[str, object], api: str) -> dict[str, object] | None:
    limit = raw.get("limit")
    limit = limit if isinstance(limit, dict) else {}
    context_window = limit.get("context")
    if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window <= 0:
        return None
    max_output_tokens = limit.get("output")
    max_input_tokens = limit.get("input")

    cost = raw.get("cost")
    cost = cost if isinstance(cost, dict) else {}

    entry: dict[str, object] = {
        "api": api,
        "context_window": context_window,
    }
    for source, target in {"tool_call": "supports_tools", "reasoning": "supports_reasoning", "structured_output": "supports_json_mode"}.items():
        value = raw.get(source)
        if isinstance(value, bool):
            entry[target] = value
    for bucket in ("input", "output", "cache_read", "cache_write"):
        rate = _per_token(cost, bucket)
        if rate is not None:
            entry[f"cost_per_{bucket}_token"] = rate
    if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool) and max_output_tokens > 0:
        entry["max_output_tokens"] = max_output_tokens
    if isinstance(max_input_tokens, int) and not isinstance(max_input_tokens, bool) and max_input_tokens > 0:
        entry["max_input_tokens"] = max_input_tokens

    display_name = raw.get("name")
    if isinstance(display_name, str) and display_name:
        entry["display_name"] = display_name

    modalities = raw.get("modalities")
    modalities_input = modalities.get("input") if isinstance(modalities, dict) else None
    if isinstance(modalities_input, list):
        entry["supports_vision"] = "image" in modalities_input
        entry["modalities_input"] = modalities_input
    modalities_output = modalities.get("output") if isinstance(modalities, dict) else None
    if isinstance(modalities_output, list):
        entry["modalities_output"] = modalities_output

    status = raw.get("status")
    if isinstance(status, str) and status:
        entry["model_status"] = status
    entry.update(_reasoning_effort_fields(raw))
    return entry


def _provider_catalog_entries(
    provider_id: str,
    keys: tuple[str, ...],
    payload: dict[str, object],
) -> dict[str, dict[str, object]]:
    """Every catalog entry one provider contributes from its upstream source keys.

    Models that are not callable, deprecated, or carry no context window are
    dropped; the model id is case-folded. Missing upstream facts stay unknown.
    """
    provider_models: dict[str, dict[str, object]] = {}
    for key in keys:
        source = payload.get(key)
        if not isinstance(source, dict):
            continue
        models = source.get("models")
        if not isinstance(models, dict):
            continue
        for model_id, raw in models.items():
            if not isinstance(model_id, str) or not isinstance(raw, dict):
                continue
            if raw.get("tool_call") is not True:
                continue
            if raw.get("status") == "deprecated":
                continue
            model_key = model_id.strip().lower()
            entry = _model_entry(raw, _model_api(provider_id, model_key, raw))
            if entry is None:
                continue
            provider_models[model_key] = entry
    return provider_models


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="local models.dev api.json; defaults to the official API")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="generated catalog destination")
    args = parser.parse_args(argv)
    source = args.source.read_bytes() if args.source is not None else _fetch_raw()
    payload = json.loads(source)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("models.dev response must be a non-empty provider object")
    catalog = {}
    for provider_id, keys in PROVIDER_KEYS.items():
        models = _provider_catalog_entries(provider_id, keys, payload)
        if models:
            catalog[provider_id] = models
    if not catalog:
        raise ValueError("models.dev response yielded no supported coding models")
    # Validate before replacing the snapshot, using the runtime's actual loader.
    from voidcode.capabilities.providers.native.model_catalog import _static_catalog_from_payload

    _static_catalog_from_payload(catalog)
    args.output.write_text(json.dumps(catalog, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(f"{CATALOG_URL}: {sum(len(models) for models in catalog.values())} models across {len(catalog)} providers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
