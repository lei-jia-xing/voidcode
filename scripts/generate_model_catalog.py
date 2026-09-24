"""Generate the bundled model catalog from the models.dev mirror.

Dev-time generator only; never imported by the runtime. Fetches the
field-pruned models.dev mirror at https://catalog.stencil.so/models.json.zstd
and emits `src/voidcode/provider/model_catalog_data.json`, which is the
runtime's only static metadata source.

Field names in the emitted per-model entries must EXACTLY match
`ProviderModelMetadata` (the loader constructs the dataclass from them).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx
import zstandard

from voidcode.provider.api_routes import api_route_for
from voidcode.provider.provider_table import PROVIDER_TABLE, PROVIDER_TABLE_BY_ID

CATALOG_URL = "https://catalog.stencil.so/models.json.zstd"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "src" / "voidcode" / "provider" / "model_catalog_data.json"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # little-endian 0xfd2fb528

# Must stay equal to `CANONICAL_EFFORTS` (src/voidcode/provider/reasoning_effort.py):
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


def _decode(content: bytes) -> dict[str, object]:
    if content[:4] == _ZSTD_MAGIC:
        content = zstandard.ZstdDecompressor().decompress(content)
    return json.loads(content)


def _per_token(cost: dict[str, object], key: str) -> float:
    # ponytail: a bucket upstream does not carry is written as 0.0, so the runtime
    # cannot tell "upstream prices this at zero" from "upstream says nothing", and a
    # partially-priced model is reported cheaper than it is (never more expensive).
    # Upgrade path: emit the bucket as absent and carry ``float | None`` through
    # ``LongContextRates``/``usage_cost_usd``, which already returns ``None`` for
    # "unpriced".
    value = cost.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1_000_000
    return 0.0


def _reasoning_effort_fields(raw: dict[str, object]) -> dict[str, object]:
    """Reasoning-effort capability from upstream `reasoning_options[]`.

    Any option means the model exposes a reasoning control, so the capability
    flag is present for every model: `True` when upstream lists options,
    `False` when it does not. The model's own levels come from the `effort`
    option's `values`, reduced to the canonical ladder and re-ordered by it;
    upstream tokens outside the ladder (`none`, `default`) are not effort
    levels, and `off` is expressed by omitting a level, not by one.

    Upstream carries no per-model default-effort signal, so
    `default_reasoning_effort` is not emitted; the runtime/frontend fall back
    to the cheapest level the model supports.
    """
    options = raw.get("reasoning_options")
    if not isinstance(options, list) or not options:
        return {"supports_reasoning_effort": False}
    fields: dict[str, object] = {"supports_reasoning_effort": True}
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
        "cost_per_input_token": _per_token(cost, "input"),
        "cost_per_output_token": _per_token(cost, "output"),
        "cost_per_cache_read_token": _per_token(cost, "cache_read"),
        "cost_per_cache_write_token": _per_token(cost, "cache_write"),
        "supports_tools": bool(raw.get("tool_call")),
        "supports_reasoning": bool(raw.get("reasoning")),
    }
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
    else:
        entry["supports_vision"] = False
        entry["modalities_input"] = ["text"]
    modalities_output = modalities.get("output") if isinstance(modalities, dict) else None
    if isinstance(modalities_output, list):
        entry["modalities_output"] = modalities_output

    status = raw.get("status")
    entry["model_status"] = status if isinstance(status, str) and status else "active"
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


def main() -> int:
    payload = _decode(_fetch_raw())
    catalog: dict[str, dict[str, dict[str, object]]] = {}
    for provider_id, keys in PROVIDER_KEYS.items():
        provider_models = _provider_catalog_entries(provider_id, keys, payload)
        if provider_models:
            catalog[provider_id] = provider_models

    OUTPUT_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for provider_id, models in catalog.items():
        print(f"{provider_id}: {len(models)} models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
