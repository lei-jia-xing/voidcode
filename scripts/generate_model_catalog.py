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
from pathlib import Path

import httpx
import zstandard

CATALOG_URL = "https://catalog.stencil.so/models.json.zstd"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "src" / "voidcode" / "provider" / "model_catalog_data.json"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # little-endian 0xfd2fb528

PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "openai": ("openai",),
    "anthropic": ("anthropic",),
    "google": ("google",),
    "deepseek": ("deepseek",),
    "grok": ("xai",),
    "qwen": ("qwen-portal", "alibaba-coding-plan"),
    "glm": ("zai", "zhipuai-coding-plan"),
    "kimi": ("moonshotai",),
    "minimax": ("minimax", "minimax-cn"),
    "opencode": ("opencode",),
    "opencode-go": ("opencode-go",),
    "copilot": ("github-copilot",),
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
    value = cost.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1_000_000
    return 0.0


def _model_entry(raw: dict[str, object]) -> dict[str, object] | None:
    limit = raw.get("limit")
    limit = limit if isinstance(limit, dict) else {}
    context_window = limit.get("context")
    if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window <= 0:
        return None
    max_output_tokens = limit.get("output")

    cost = raw.get("cost")
    cost = cost if isinstance(cost, dict) else {}

    entry: dict[str, object] = {
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

    modalities = raw.get("modalities")
    modalities_input = modalities.get("input") if isinstance(modalities, dict) else None
    if isinstance(modalities_input, list):
        entry["supports_vision"] = "image" in modalities_input
        entry["modalities_input"] = modalities_input
    else:
        entry["supports_vision"] = False
        entry["modalities_input"] = ["text"]

    status = raw.get("status")
    entry["model_status"] = status if isinstance(status, str) and status else "active"
    return entry


def main() -> int:
    payload = _decode(_fetch_raw())
    catalog: dict[str, dict[str, dict[str, object]]] = {}
    for provider_id, keys in PROVIDER_KEYS.items():
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
                entry = _model_entry(raw)
                if entry is None:
                    continue
                provider_models[model_id.strip().lower()] = entry
        if provider_models:
            catalog[provider_id] = provider_models

    OUTPUT_PATH.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for provider_id, models in catalog.items():
        print(f"{provider_id}: {len(models)} models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
