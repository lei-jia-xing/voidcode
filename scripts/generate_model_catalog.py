"""Generate the bundled model catalog from the models.dev mirror.

Dev-time generator only; never imported by the runtime. Fetches the
field-pruned models.dev mirror at https://catalog.stencil.so/models.json.zstd
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
from pathlib import Path
from typing import Final

import httpx
import zstandard

from voidcode.capabilities.providers.native.api_routes import api_route_for
from voidcode.capabilities.providers.native.provider_table import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID

CATALOG_URL = "https://catalog.stencil.so/models.json.zstd"
_PROVIDER_PLUGIN = Path(__file__).resolve().parent.parent / "src" / "voidcode" / "capabilities" / "providers" / "native"
OUTPUT_PATH = _PROVIDER_PLUGIN / "model_catalog_data.json"
#: Model -> tokenizer routing, extracted from oh-my-pi by
#: ``scripts/extract_tokenizer_data.py``. The models.dev mirror carries no
#: ``tokenizer`` field, so this extract is the only source of that identity.
TOKENIZER_ROUTING_PATH = _PROVIDER_PLUGIN / "tokenizer_routing.json"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # little-endian 0xfd2fb528

# Must stay equal to `CANONICAL_EFFORTS`
# (src/voidcode/capabilities/providers/native/reasoning_effort.py):
# the runtime clamps requested effort against these levels, so a level outside
# the ladder would be unreachable, and a missing one would never be offered.
CANONICAL_EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")

# Canonical provider id -> the upstream models.dev keys its catalog is built
# from, straight from the one provider table. A provider with no upstream key
# (a runtime-only gateway) contributes no catalog entry.
PROVIDER_KEYS: dict[str, tuple[str, ...]] = {row.id: row.models_dev_keys for row in PROVIDER_TABLE if row.models_dev_keys}

#: Models whose default reasoning effort is authored, not derived: upstream
#: carries no per-model default signal (no `default` key anywhere in
#: `reasoning_options[]`), so `_reasoning_effort_fields` cannot produce these.
#: The eight values below were hand-set in commit 153a9a95 and are preserved
#: here so regenerating the catalog does not silently drop them; Wave W3 owns
#: the final default-effort semantics.
DEFAULT_REASONING_EFFORT: dict[tuple[str, str], str] = {
    ("github-copilot", "kimi-k3"): "max",
    ("moonshot", "kimi-k3"): "max",
    ("opencode-zen", "kimi-k3"): "max",
    ("opencode-go", "kimi-k3"): "max",
    ("zai", "glm-5.3"): "max",
    ("zhipuai", "glm-5.3"): "max",
    ("opencode-go", "glm-5.3"): "max",
    ("opencode-go", "glm-5.3-flash"): "max",
}


def _fetch_raw() -> bytes:
    response = httpx.get(
        CATALOG_URL,
        headers={
            "Accept": "application/zstd, application/json",
            "User-Agent": "voidcode-model-catalog-generator",
        },
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def _tokenizer_routing() -> dict[tuple[str, str], str]:
    """Model -> tokenizer name, from the checked-in omp routing extract.

    The models.dev mirror carries NO ``tokenizer`` field (verified against both
    the local snapshot and the live endpoint), so the identity cannot come from
    upstream. It comes from oh-my-pi's own per-model routing table, extracted to
    ``provider/tokenizer_routing.json`` by
    ``scripts/extract_tokenizer_data.py`` -- the same generator that ships the
    vocabularies. Keys are ``(provider_id, model_id)``, both lower-case.
    """
    if not TOKENIZER_ROUTING_PATH.exists():
        raise SystemExit(f"missing {TOKENIZER_ROUTING_PATH}; run scripts/extract_tokenizer_data.py first")
    raw = json.loads(TOKENIZER_ROUTING_PATH.read_text(encoding="utf-8"))
    return {(provider.lower(), model.lower()): name for provider, models in raw.items() for model, name in models.items()}


def _decode(content: bytes) -> dict[str, object]:
    if content[:4] == _ZSTD_MAGIC:
        content = zstandard.ZstdDecompressor().decompress(content)
    return json.loads(content)


def _per_token(cost: dict[str, object], key: str) -> float | None:
    value = cost.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
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
        levels = [level for level in CANONICAL_EFFORT_LEVELS if level in values]
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
    for key in ("tool_call", "reasoning"):
        value = raw.get(key)
        if isinstance(value, bool):
            entry["supports_tools" if key == "tool_call" else "supports_reasoning"] = value
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
    dropped; the model id is case-folded. An authored default effort
    (``DEFAULT_REASONING_EFFORT``) is the last thing applied.
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
            default_effort = DEFAULT_REASONING_EFFORT.get((provider_id, model_key))
            if default_effort is not None:
                entry["default_reasoning_effort"] = default_effort
            provider_models[model_key] = entry
    return provider_models


def _refresh_existing_prices(catalog: dict[str, dict[str, dict[str, object]]], payload: dict[str, object]) -> int:
    refreshed = 0
    for provider_id, models in catalog.items():
        source_models: dict[str, dict[str, object]] = {}
        for key in PROVIDER_KEYS.get(provider_id, ()):
            provider = payload.get(key)
            raw_models = provider.get("models") if isinstance(provider, dict) else None
            if isinstance(raw_models, dict):
                source_models.update(
                    (model_id.strip().lower(), raw) for model_id, raw in raw_models.items() if isinstance(model_id, str) and isinstance(raw, dict)
                )
        for model_id, entry in models.items():
            raw = source_models.get(model_id)
            if raw is None:
                continue
            cost = raw.get("cost")
            cost = cost if isinstance(cost, dict) else {}
            for bucket in ("input", "output", "cache_read", "cache_write"):
                field = f"cost_per_{bucket}_token"
                rate = _per_token(cost, bucket)
                if rate is None:
                    entry.pop(field, None)
                else:
                    entry[field] = rate
            refreshed += 1
    return refreshed


def _tag_tokenizers(catalog: dict[str, dict[str, dict[str, object]]], routing: Mapping[tuple[str, str], str]) -> int:
    """Stamp ``tokenizer`` onto every catalog entry omp routes to an encoding."""
    tagged = 0
    for provider_id, models in catalog.items():
        for model_key, entry in models.items():
            name = routing.get((provider_id, model_key))
            if name is None:
                entry.pop("tokenizer", None)
                continue
            entry["tokenizer"] = name
            tagged += 1
    return tagged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="local models.json.zstd or JSON snapshot; defaults to the canonical mirror")
    parser.add_argument("--retag-only", action="store_true", help="re-stamp tokenizers without reading the mirror")
    parser.add_argument("--pricing-only", action="store_true", help="refresh price fields on existing rows, preserving all other catalog data")
    args = parser.parse_args(argv)
    if args.retag_only and (args.source is not None or args.pricing_only):
        parser.error("--retag-only cannot be combined with --source or --pricing-only")
    routing = _tokenizer_routing()

    if args.retag_only:
        catalog = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    else:
        source = args.source.read_bytes() if args.source is not None else _fetch_raw()
        payload = _decode(source)
        if args.pricing_only:
            catalog = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
            refreshed = _refresh_existing_prices(catalog, payload)
        else:
            catalog = {}
            for provider_id, keys in PROVIDER_KEYS.items():
                provider_models = _provider_catalog_entries(provider_id, keys, payload)
                if provider_models:
                    catalog[provider_id] = provider_models

    tagged = (
        _tag_tokenizers(catalog, routing)
        if not args.pricing_only
        else sum("tokenizer" in entry for models in catalog.values() for entry in models.values())
    )
    OUTPUT_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total = sum(len(models) for models in catalog.values())
    if not args.retag_only and not args.pricing_only:
        for provider_id, models in catalog.items():
            print(f"{provider_id}: {len(models)} models")
    if args.pricing_only:
        print(f"pricing provenance: {args.source if args.source is not None else CATALOG_URL}; refreshed {refreshed} existing rows")
    else:
        print(f"tokenizer routing: {tagged}/{total} catalog entries tagged ({tagged / total:.1%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
